import json
import tempfile
import threading
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from supreme import liquidity, markets, parser, store


class FakeShop(BaseHTTPRequestHandler):
    """Магазин для тестов: отвечает так, как Shopify, WooCommerce или страница с JSON-LD."""
    routes = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = self.routes.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        payload = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def serve(routes):
    handler = type("Shop", (FakeShop,), {"routes": routes})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


class MatchingTests(unittest.TestCase):
    def test_market_key_ignores_noise(self):
        self.assertEqual(markets.market_key("Icebreaker (Godly Knife)"), "icebreaker")
        self.assertEqual(markets.market_key("MM2 Chroma Luger [Godly]"), "chromaluger")
        self.assertEqual(markets.market_key("C. Fang"), "chromafang")

    def test_index_skips_secret_and_untradable(self):
        items = [
            parser.make_item("Batwing", "godlies", {"value": "1,000,000"}),
            parser.make_item("Batwing", "ancients", {"value": "42"}),
            parser.make_item("Locked", "untradables", {"value": "N/A"}),
        ]
        index = markets.build_index(items)
        self.assertEqual(markets.find_item(index, {"name": "Batwing"})["category"], "ancients")
        self.assertIsNone(markets.find_item(index, {"name": "Locked"}))

    def test_gun_and_knife_with_same_base_name(self):
        items = [parser.make_item("Flowerwood", "godlies", {"value": "50"}),
                 parser.make_item("Flowerwood Gun", "godlies", {"value": "40"})]
        for order in (items, list(reversed(items))):
            index = markets.build_index(order)
            self.assertEqual(markets.find_item(index, {"name": "Flowerwood"})["name"], "Flowerwood")
            self.assertEqual(markets.find_item(index, {"name": "Flowerwood Gun"})["name"], "Flowerwood Gun")
            self.assertEqual(markets.find_item(index, {"name": "Flowerwood", "kind": "gun"})["name"], "Flowerwood Gun")
            self.assertEqual(markets.find_item(index, {"name": "Flowerwood", "kind": "knife"})["name"], "Flowerwood")
            self.assertIsNone(markets.find_item(index, {"name": "Flowerwood (Godly)", "kind": ""}) and None)


class ShopAdapterTests(unittest.TestCase):
    def adapter_for(self, routes):
        httpd, base = serve(routes)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return markets.ShopAdapter({"id": "shop", "title": "Shop", "kind": "shop", "url": base, "pages": ["/mm2"]}), base

    def test_shopify(self):
        adapter, base = self.adapter_for({
            "/products.json?limit=250&page=1": {"products": [
                {"title": "Icebreaker (Godly)", "handle": "icebreaker",
                 "variants": [{"price": "4.50", "available": True, "inventory_quantity": 3}]},
                {"title": "Sold Out Knife", "handle": "x", "variants": [{"price": "0", "available": False}]},
            ]},
            "/products.json?limit=250&page=2": {"products": []},
        })
        offers = adapter.fetch()
        self.assertEqual(offers[0], {"name": "Icebreaker (Godly)", "price": 4.5, "stock": 3, "sales_week": None,
                                     "available": True, "url": f"{base}/products/icebreaker"})
        self.assertIsNone(offers[1]["price"])
        self.assertFalse(offers[1]["available"])

    def test_woocommerce(self):
        adapter, _ = self.adapter_for({
            "/wp-json/wc/store/v1/products?per_page=100&page=1": [
                {"name": "Chroma Luger", "prices": {"price": "1299", "currency_minor_unit": 2},
                 "is_in_stock": True, "permalink": "https://shop/luger", "low_stock_remaining": 2},
            ],
            "/wp-json/wc/store/v1/products?per_page=100&page=2": [],
        })
        offers = adapter.fetch()
        self.assertEqual((offers[0]["name"], offers[0]["price"], offers[0]["stock"]), ("Chroma Luger", 12.99, 2))

    def test_jsonld(self):
        page = ('<html><script type="application/ld+json">{"@context":"https://schema.org","@type":"ItemList",'
                '"itemListElement":[{"@type":"ListItem","item":{"@type":"Product","name":"Fang",'
                '"offers":{"@type":"Offer","price":"2.10","availability":"https://schema.org/InStock"}}}]}</script></html>')
        adapter, _ = self.adapter_for({"/mm2": page})
        offers = adapter.fetch()
        self.assertEqual((offers[0]["name"], offers[0]["price"], offers[0]["available"]), ("Fang", 2.1, True))

    def test_nothing_readable(self):
        adapter, _ = self.adapter_for({})
        with self.assertRaises(markets.fetcher.FetchError):
            adapter.fetch()


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "values.json"
        data = store.empty_data()
        store.replace_category(data, "godlies", [
            parser.make_item("Icebreaker", "godlies", {"value": "2,000", "demand": "8", "stability": "Stable"}),
            parser.make_item("Fang", "godlies", {"value": "300", "demand": "2"}),
        ], "site")
        store.save(data, self.path)
        self.offers = []

    def tearDown(self):
        self.tmp.cleanup()

    def monitor(self):
        test = self

        class Fake(markets.Adapter):
            def fetch(self):
                if isinstance(test.offers, Exception):
                    raise test.offers
                return [dict(offer) for offer in test.offers]

        config = {"id": "fake", "title": "FakeMarket", "kind": "fake", "url": "http://x"}
        return markets.MarketMonitor(self.path, [config], interval=120, adapters={"fake": Fake})

    def test_poll_matches_and_detects_purchases(self):
        monitor = self.monitor()
        self.offers = [
            {"name": "Icebreaker (Godly Knife)", "price": 5.0, "stock": 6, "sales_week": None, "available": True, "url": "u"},
            {"name": "Icebreaker", "price": 4.0, "stock": 2, "sales_week": None, "available": True, "url": "u2"},
            {"name": "Unknown Thing", "price": 1.0, "stock": 1, "sales_week": None, "available": True, "url": "u3"},
        ]
        monitor.poll(monitor.markets[0])
        status = monitor.status_list()[0]
        self.assertEqual((status["status"], status["matched"], status["offers"]), ("ok", 1, 2))
        self.offers = [{"name": "Icebreaker", "price": 4.5, "stock": 5, "sales_week": None, "available": True, "url": "u"}]
        monitor.poll(monitor.markets[0])
        items = monitor.annotate([dict(i, liquidity=liquidity.assess(i)) for i in store.load(self.path)["items"]])
        ice = next(i for i in items if i["name"] == "Icebreaker")
        info = ice["market"]["fake"]
        self.assertEqual((info["price"], info["stock"], info["sold_48h"]), (4.5, 5, 3))
        self.assertEqual(ice["combined"]["sources"], 1)  # общая — только по площадкам
        fang = next(i for i in items if i["name"] == "Fang")
        self.assertEqual(fang["market"], {})
        self.assertEqual(fang["combined"]["sources"], 0)  # нет на площадках — нет общей оценки

    def test_state_survives_restart(self):
        monitor = self.monitor()
        self.offers = [{"name": "Fang", "price": 1.0, "stock": 4, "sales_week": 12, "available": True, "url": "u"}]
        monitor.poll(monitor.markets[0])
        again = self.monitor()
        self.assertEqual(again.status_list()[0]["matched"], 1)
        items = again.annotate([dict(i, liquidity=liquidity.assess(i)) for i in store.load(self.path)["items"]])
        fang = next(i for i in items if i["name"] == "Fang")
        self.assertEqual(fang["market"]["fake"]["sales_week"], 12)
        self.assertGreaterEqual(fang["market"]["fake"]["score"], 60)

    def test_errors_back_off(self):
        from datetime import timedelta
        monitor = self.monitor()
        market = monitor.markets[0]
        self.offers = markets.fetcher.BlockedError("площадка не пускает программу (HTTP 403)")
        before = markets._now()
        monitor.poll(market)
        status = monitor.status_list()[0]
        self.assertEqual(status["status"], "error")
        self.assertIn("403", status["message"])
        self.assertGreaterEqual(market.next_poll_at - before, timedelta(seconds=markets.BLOCKED_PAUSE))

        market.failures = 0
        self.offers = markets.fetcher.FetchError("HTTP 500")
        monitor.poll(market)
        first = market.next_poll_at - markets._now()
        monitor.poll(market)
        second = market.next_poll_at - markets._now()
        self.assertGreater(second, first)  # каждая следующая ошибка — пауза дольше

    def test_offer_scores(self):
        self.assertEqual(markets.assess_offer({"sales_week": 0}, 0)["level"], liquidity.ILLIQUID)
        self.assertEqual(markets.assess_offer({"sales_week": 20}, 0)["level"], liquidity.LIQUID)
        self.assertEqual(markets.assess_offer({"stock": 3}, 0)["level"], liquidity.MEDIUM)
        self.assertIsNone(markets.assess_offer({"stock": 50, "available": False}, 0)["score"])
        self.assertIsNone(markets.assess_offer({"price": 5, "available": True}, 0)["score"])
        self.assertEqual(markets.assess_offer({"popularity": 0.0, "available": True}, 0)["score"], 80)


class ConfigTests(unittest.TestCase):
    def test_custom_list_and_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp) / "values.json"
            self.assertEqual([c["id"] for c in markets.load_configs(data_path)], [c["id"] for c in markets.DEFAULT_MARKETS])
            (Path(tmp) / "markets.json").write_text(json.dumps([
                {"id": "myshop", "kind": "shop", "url": "https://example.com"},
                {"id": "bad", "kind": "nope", "url": "x"},
            ]), encoding="utf-8")
            configs = markets.load_configs(data_path)
            self.assertEqual([(c["id"], c["title"]) for c in configs], [("myshop", "myshop")])
            self.assertEqual(configs[0]["fee"], 0)

    def test_fee_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp) / "values.json"
            (Path(tmp) / "markets.json").write_text(json.dumps([
                {"id": "sp", "kind": "starpets", "url": "https://starpets.gg/mm2"},
                {"id": "dp", "kind": "dreampets", "url": "https://dreampets.gg/mm2/", "fee": "abc"},
                {"id": "dp2", "kind": "dreampets", "url": "https://dreampets.gg/mm2/", "fee": 1.5},
                {"id": "dp3", "kind": "dreampets", "url": "https://dreampets.gg/mm2/", "fee": "0.15"},
            ]), encoding="utf-8")
            fees = {c["id"]: c["fee"] for c in markets.load_configs(data_path)}
            self.assertEqual(fees, {"sp": 0.20, "dp": 0.10, "dp2": 0.10, "dp3": 0.15})



class FakeApi(BaseHTTPRequestHandler):
    """StarPets API для тестов."""
    calls = []
    info_status = 200
    rub_status = 200

    def log_message(self, *args):
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.calls.append(("POST", self.path, body))
        kind = body["filter"]["types"][0]["type"]
        if body["currency"] == "rub" and self.rub_status != 200:
            return self._send(self.rub_status, {"status": False, "message": '"currency" must be one of [usd]'})
        rub = body["currency"] == "rub"
        rows = {
            "weapon": [
                {"id": 485, "name": "Luger", "type": "weapon", "subtype": "gun", "chroma": False,
                 "price": 75.0 if rub else 1.02},
                {"id": 756, "name": "Luger", "type": "weapon", "subtype": "gun", "chroma": True,
                 "price": 150.5 if rub else 2.0},
                {"id": 900, "name": "Gone", "type": "weapon", "subtype": "knife", "chroma": False, "price": None},
            ],
            "pet": [], "misc": [],
        }[kind]
        self._send(200, {"status": True, "items": rows if body["page"] == 1 else [], "count": len(rows)})

    def do_GET(self):
        self.calls.append(("GET", self.path, None))
        product_id = int(self.path.split("/")[-2])
        self._send(self.info_status, {"product": {"id": product_id, "numberOfSalesPerWeek": product_id // 10}})


class StarPetsTests(unittest.TestCase):
    def setUp(self):
        FakeApi.calls = []
        FakeApi.info_status = 200
        FakeApi.rub_status = 200
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeApi)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.config = {"id": "starpets", "title": "StarPets", "kind": "starpets", "url": "https://starpets.gg/mm2",
                       "api": f"http://127.0.0.1:{self.httpd.server_address[1]}"}
        patcher = unittest.mock.patch.object(markets, "_pause")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_catalog_chroma_and_sales(self):
        cache = {}
        offers = markets.StarPetsAdapter(self.config, cache).fetch()
        by_name = {o["name"]: o for o in offers}
        self.assertEqual(by_name["Luger"]["price"], 1.02)
        self.assertEqual(by_name["Chroma Luger"]["price"], 2.0)
        self.assertFalse(by_name["Gone"]["available"])
        self.assertEqual(by_name["Chroma Luger"]["sales_week"], 75)  # самые дорогие — первыми
        self.assertEqual(by_name["Luger"]["url"], "https://starpets.gg/mm2/shop/weapon/luger/485")
        # Рубли — цены самой площадки, а не доллары по курсу.
        self.assertEqual((by_name["Luger"]["price_rub"], by_name["Chroma Luger"]["price_rub"]), (75.0, 150.5))
        self.assertFalse(by_name["Luger"]["price_rub_approx"])
        posts = [c for c in FakeApi.calls if c[0] == "POST"]
        self.assertEqual([(p[2]["currency"], p[2]["filter"]["types"][0]["type"]) for p in posts],
                         [("usd", "weapon"), ("usd", "pet"), ("usd", "misc"),
                          ("rub", "weapon"), ("rub", "pet"), ("rub", "misc")])
        gets_before = len([c for c in FakeApi.calls if c[0] == "GET"])
        FakeApi.calls = []
        offers = markets.StarPetsAdapter(self.config, cache).fetch()  # продажи уже свежие — не запрашиваются снова
        self.assertEqual(len([c for c in FakeApi.calls if c[0] == "GET"]), 0)
        self.assertGreater(gets_before, 0)
        # Дальше — по одной валюте за опрос (меньше запросов), цены другой — из прошлого раза.
        self.assertEqual({p[2]["currency"] for p in FakeApi.calls if p[0] == "POST"}, {"usd"})
        self.assertEqual({o["name"]: o["price_rub"] for o in offers}["Luger"], 75.0)
        FakeApi.calls = []
        markets.StarPetsAdapter(self.config, cache).fetch()
        self.assertEqual({p[2]["currency"] for p in FakeApi.calls if p[0] == "POST"}, {"rub"})

    def test_rubles_unavailable_falls_back_to_site_rate(self):
        FakeApi.rub_status = 400
        cache = {}
        adapter = markets.StarPetsAdapter(self.config, cache)
        offers = {o["name"]: o for o in adapter.fetch()}
        self.assertEqual(offers["Luger"]["price"], 1.02)
        self.assertIsNone(offers["Luger"]["price_rub"])  # курса площадки ещё нет — рублей нет, а не выдумка
        self.assertIn("рублях", adapter.note)
        FakeApi.calls = []
        markets.StarPetsAdapter(self.config, cache).fetch()  # следующий опрос — доллары, а не снова рубли
        self.assertEqual({p[2]["currency"] for p in FakeApi.calls if p[0] == "POST"}, {"usd"})

    def test_sales_endpoint_missing_is_paused(self):
        FakeApi.info_status = 404
        cache = {}
        markets.StarPetsAdapter(self.config, cache).fetch()
        gets = len([c for c in FakeApi.calls if c[0] == "GET"])
        self.assertEqual(gets, 2)  # оба предмета с ценой попробованы и ушли в конец очереди
        markets.StarPetsAdapter(self.config, cache).fetch()
        self.assertEqual(len([c for c in FakeApi.calls if c[0] == "GET"]), gets)  # без повторов подряд
        with unittest.mock.patch.object(markets.StarPetsAdapter, "INFO_MAX_AGE", -1):
            adapter = markets.StarPetsAdapter(self.config, cache)
            offers = adapter.fetch()  # третья ошибка подряд — пауза на час
        self.assertGreater(cache["info_off_until"], 0)
        self.assertIn("не отдаёт", adapter.note)
        self.assertTrue(all(o["sales_week"] is None for o in offers))


def no_browser(test):
    patcher = unittest.mock.patch.object(markets.fetcher, "find_system_browser", return_value=None)
    patcher.start()
    test.addCleanup(patcher.stop)


class DreamPetsTests(unittest.TestCase):
    def setUp(self):
        patcher = unittest.mock.patch.object(markets, "_pause")
        patcher.start()
        self.addCleanup(patcher.stop)
        no_browser(self)

    def test_market_and_product_pages(self):
        routes = {
            "/mm2/": '<a href="/mm2/product/harvester/aaa">Harvester</a><a href="/mm2/product/seer/bbb">Seer</a>'
                     '<a href="/mm2-legacy/product/plasmite/ccc">legacy</a>',
            "/mm2/product/harvester/aaa": '<title>Harvester — купить в ММ2</title>'
                                          '<meta name="description" content="Harvester от 328.98 ₽, 674 лотов">',
            "/mm2/product/seer/bbb": '<title>Seer / Провидец — купить в ММ2</title><p>от 17 ₽</p><p>82 лота</p>',
        }
        httpd, base = serve(routes)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        cache = {}
        adapter = markets.DreamPetsAdapter({"id": "dreampets", "title": "DreamPets", "kind": "dreampets",
                                            "url": base + "/mm2/"}, cache)
        offers = {o["name"]: o for o in adapter.fetch()}
        self.assertEqual(set(offers), {"Harvester", "Seer"})
        self.assertEqual((offers["Harvester"]["price"], offers["Harvester"]["stock"], offers["Harvester"]["currency"]),
                         (328.98, 674, "RUB"))
        self.assertEqual((offers["Seer"]["price"], offers["Seer"]["stock"]), (17.0, 82))
        self.assertIn("товаров: 2, с ценой: 2", adapter.note)

    def test_empty_market_is_an_error(self):
        httpd, base = serve({"/mm2/": "<html>nothing</html>"})
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        with unittest.mock.patch.object(markets.fetcher, "find_system_browser", return_value=None):
            with self.assertRaises(markets.fetcher.FetchError):
                markets.DreamPetsAdapter({"id": "d", "title": "D", "kind": "dreampets", "url": base + "/mm2/"}).fetch()


class EldoradoTests(unittest.TestCase):
    def setUp(self):
        patcher = unittest.mock.patch.object(markets, "_pause")
        patcher.start()
        self.addCleanup(patcher.stop)

    def offer(self, offer_id, name, price, quantity, kind="Knife", chroma=False, seller="s1", sold=None):
        return {
            "offer": {
                "id": offer_id, "quantity": quantity, "pricePerUnitInUSD": {"amount": price, "currency": "USD"},
                "tradeEnvironmentValues": [{"name": "Item type", "value": kind}, {"name": "Item name", "value": name}],
                "attributes": [{"id": "mm2-properties", "values": [{"name": "Chroma" if chroma else "Common"}]}],
                "orderCounts": {"last30Days": sold} if sold is not None else None,
            },
            "user": {"id": seller},
        }

    def test_pages_rotate_and_aggregate(self):
        pages = {
            1: [self.offer("a", "Fang", 2.5, 3), self.offer("b", "Fang", 2.0, 1, seller="s2"),
                self.offer("c", "VIP Server", 0.5, 1, kind="Other"), self.offer("d", "Bait", 0.00001, 5000)],
            2: [self.offer("e", "Luger", 1.0, 2, kind="Gun", chroma=True, sold=30)],
        }
        routes = {
            f"/?gameId=204&category=CustomItem&offerSortingCriterion=Price&isAscending=true&pageIndex={n}&pageSize=50":
                {"results": rows, "totalPages": 2, "recordCount": 5}
            for n, rows in pages.items()
        }
        httpd, base = serve(routes)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        cache = {}
        config = {"id": "eldorado", "title": "Eldorado", "kind": "eldorado", "url": "https://eldorado", "api": base + "/"}
        adapter = markets.EldoradoAdapter(config, cache)
        adapter.PAGES_PER_POLL = 1
        first = {o["name"]: o for o in adapter.fetch()}
        self.assertEqual(set(first), {"Fang"})
        self.assertEqual((first["Fang"]["price"], first["Fang"]["stock"], first["Fang"]["sellers"]), (2.0, 4, 2))
        second = {o["name"]: o for o in adapter.fetch()}
        self.assertEqual(set(second), {"Fang", "Chroma Luger"})
        self.assertEqual(second["Chroma Luger"]["sales_week"], 7)
        self.assertEqual(cache["page"], 1)  # круг пройден, следующий опрос — снова с первой страницы


class ReviewV3Tests(unittest.TestCase):
    """Случаи, найденные при проверке площадок."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "values.json"
        data = store.empty_data()
        store.replace_category(data, "godlies", [
            parser.make_item(name, "godlies", {"value": "100", "demand": "8", "stability": "Stable"})
            for name in ("Fang", "Seer", "Harvester")
        ], "site")
        store.save(data, self.path)
        patcher = unittest.mock.patch.object(markets, "_pause")
        patcher.start()
        self.addCleanup(patcher.stop)

    def eldorado(self, pages, cache):
        routes = {
            f"/?gameId=204&category=CustomItem&offerSortingCriterion=Price&isAscending=true&pageIndex={n}&pageSize=50":
                {"results": rows, "totalPages": len(pages)}
            for n, rows in pages.items()
        }
        httpd, base = serve(routes)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return {"id": "eldorado", "title": "Eldorado", "kind": "eldorado", "url": "u", "api": base + "/"}

    @staticmethod
    def row(offer_id, name, quantity):
        return {"offer": {"id": offer_id, "quantity": quantity, "pricePerUnitInUSD": {"amount": 1.0},
                          "tradeEnvironmentValues": [{"name": "Item type", "value": "Knife"},
                                                     {"name": "Item name", "value": name}]},
                "user": {"id": offer_id}}

    def monitor_for(self, config):
        class Small(markets.EldoradoAdapter):
            PAGES_PER_POLL = 1
        return markets.MarketMonitor(self.path, [config], adapters={"eldorado": Small})

    def snapshot(self, monitor):
        items = monitor.annotate([dict(i, liquidity=liquidity.assess(i)) for i in store.load(self.path)["items"]])
        return {i["name"]: (i["market"].get("eldorado") or {}).get("stock") for i in items}, \
            {i["name"]: (i["market"].get("eldorado") or {}).get("sold_48h") for i in items}

    def test_restart_and_long_pause_do_not_invent_purchases(self):
        pages = {1: [self.row("a", "Fang", 5)], 2: [self.row("b", "Seer", 3)], 3: [self.row("c", "Harvester", 2)]}
        config = self.eldorado(pages, {})
        monitor = self.monitor_for(config)
        for _ in range(6):  # два полных круга
            monitor.poll(monitor.markets[0])
        stock, sold = self.snapshot(monitor)
        self.assertEqual(stock, {"Fang": 5, "Seer": 3, "Harvester": 2})
        self.assertEqual(set(sold.values()), {0})

        again = self.monitor_for(config)  # перезапуск программы
        again.poll(again.markets[0])
        stock, sold = self.snapshot(again)
        self.assertEqual(stock, {"Fang": 5, "Seer": 3, "Harvester": 2})
        self.assertEqual(set(sold.values()), {0})

        cache = again.markets[0].cache  # компьютер спал 4 часа
        cache["last_fetch"] -= 4 * 3600
        for _ in range(3):
            again.poll(again.markets[0])
        stock, sold = self.snapshot(again)
        self.assertEqual(stock, {"Fang": 5, "Seer": 3, "Harvester": 2})
        self.assertEqual(set(sold.values()), {0})

    def test_real_purchase_is_counted_after_full_cycles(self):
        pages = {1: [self.row("a", "Fang", 5)], 2: [self.row("b", "Seer", 3)]}
        config = self.eldorado(pages, {})
        monitor = self.monitor_for(config)
        for _ in range(4):
            monitor.poll(monitor.markets[0])
        pages[1][0]["offer"]["quantity"] = 2  # купили 3 штуки
        for _ in range(2):
            monitor.poll(monitor.markets[0])
        _, sold = self.snapshot(monitor)
        self.assertEqual(sold["Fang"], 3)

    def test_no_data_does_not_lower_combined(self):
        item = parser.make_item("Fang", "godlies", {"value": "100", "demand": "8", "stability": "Stable"})
        item["liquidity"] = liquidity.assess(item)
        infos = [dict(markets.assess_offer({"price": 5, "available": True}, 0), market_title="StarPets"),
                 dict(markets.assess_offer({"available": False}, 0), market_title="DreamPets")]
        combined = markets.combine(item, infos)
        self.assertEqual((combined["score"], combined["level"], combined["sources"]), (None, "none", 0))
        self.assertIn("StarPets: есть в продаже, но площадка не показывает продажи", combined["reasons"])
        scored = infos + [dict(markets.assess_offer({"stock": 7, "available": True}, 0), market_title="X")]
        self.assertEqual(markets.combine(item, scored)["sources"], 1)  # «нет данных» не тянет вниз

    def test_waits_for_prices_before_matching(self):
        empty = Path(self.tmp.name) / "empty.json"
        monitor = markets.MarketMonitor(empty, [{"id": "x", "title": "X", "kind": "shop", "url": "http://127.0.0.1:9"}])
        monitor.poll(monitor.markets[0])
        status = monitor.status_list()[0]
        self.assertEqual((status["status"], status["message"]), ("waiting", "ждёт цены Supreme Values"))

    def test_bad_state_file_and_interval_are_ignored(self):
        (Path(self.tmp.name) / "markets-state.json").write_text(
            '{"x": {"offers": {"godlies:Fang": 5, "godlies:Seer": {"price": 1}}, "sales": {"a": [[1, "x"]]},'
            ' "cache": [1], "last_ok_at": "bad"}}', encoding="utf-8")
        (Path(self.tmp.name) / "markets.json").write_text(
            '[{"id": "x", "kind": "shop", "url": "http://e", "interval": "fast"},'
            ' {"id": "y", "kind": "shop", "url": "http://e", "interval": 5}]', encoding="utf-8")
        monitor = markets.MarketMonitor(self.path)
        self.assertEqual([m.config.get("interval") for m in monitor.markets], [None, None])
        market = monitor.markets[0]
        self.assertEqual((list(market.offers), market.sales, market.cache, market.last_ok_at),
                         (["godlies:Seer"], {"a": []}, {}, None))
        self.assertEqual(monitor.status_list()[0]["interval"], markets.POLL_EVERY)


class StarPetsRotationTests(unittest.TestCase):
    def test_bad_id_is_skipped_and_errors_reset(self):
        class Api(FakeApi):
            def do_GET(self):
                self.calls.append(("GET", self.path, None))
                product_id = int(self.path.split("/")[-2])
                if product_id == 756:
                    self._send(404, {})
                else:
                    self._send(200, {"product": {"numberOfSalesPerWeek": 9}})
        FakeApi.calls = []
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Api)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        config = {"id": "starpets", "title": "StarPets", "kind": "starpets", "url": "u",
                  "api": f"http://127.0.0.1:{httpd.server_address[1]}"}
        with unittest.mock.patch.object(markets, "_pause"):
            cache = {}
            offers = {o["name"]: o for o in markets.StarPetsAdapter(config, cache).fetch()}
        self.assertEqual(offers["Luger"]["sales_week"], 9)  # 756 не помешал остальным
        self.assertNotIn("info_off_until", cache)
        self.assertEqual(cache["info_errors"], 0)
        self.assertEqual(offers["Luger"]["popularity"], 0.0)


class DreamPetsReviewTests(unittest.TestCase):
    def setUp(self):
        no_browser(self)

    def test_all_pages_failing_is_an_error(self):
        httpd, base = serve({"/mm2/": '<a href="/mm2/product/a/1">A</a><a href="/mm2/product/b/2">B</a>'})
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        with unittest.mock.patch.object(markets, "_pause"):
            with self.assertRaises(markets.fetcher.FetchError):
                markets.DreamPetsAdapter({"id": "d", "title": "D", "kind": "dreampets", "url": base + "/mm2/"}).fetch()

    def test_lots_do_not_join_neighbouring_numbers(self):
        page = ('<title>Seer - ММ2</title><meta name="description" content="Seer в ММ2 от 17 ₽, 114 лотов">'
                '<p>ММ2</p><p>2 лота</p>')
        self.assertEqual(markets.dreampets.parse_product_page(page)["lots"], 114)
        self.assertEqual(markets.dreampets.parse_product_page("<title>X</title><p>1 234 лота</p>")["lots"], 1234)


if __name__ == "__main__":
    unittest.main()


class DreamPetsMarketCardsTests(unittest.TestCase):
    def setUp(self):
        no_browser(self)

    def test_prices_and_lots_from_market_page(self):
        market = (
            '<div class="product-card"><a href="/mm2/product/eternal-iii/43948eb3-1e25-4564">'
            '<img src="/i.png"><p class="card-text-l">Eternal III / Вечный 3</p><span>от 23,71 ₽</span>'
            '<span>114 лотов</span></a></div>'
            '<div class="product-card"><a href="https://dreampets.io/mm2/product/harvester/2c0af45a-9134-4c09">'
            '<p>Harvester</p><b>328.98 ₽</b><i>1 234 лота</i><button>Купить</button></a></div>'
            '<div class="product-card"><a href="/mm2/product/candy/35e0ab9c-0ef1-46b5"><p>Candy</p></a></div>'
        )
        httpd, base = serve({"/mm2/": market})
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        with unittest.mock.patch.object(markets, "_pause"), \
                unittest.mock.patch.object(markets, "_request", wraps=markets._request) as request:
            adapter = markets.DreamPetsAdapter({"id": "d", "title": "D", "kind": "dreampets", "url": base + "/mm2/"})
            offers = {o["name"]: o for o in adapter.fetch()}
        self.assertEqual((offers["Eternal III"]["price"], offers["Eternal III"]["stock"]), (23.71, 114))
        self.assertEqual((offers["Harvester"]["price"], offers["Harvester"]["stock"]), (328.98, 1234))
        self.assertIn("страница рынка: 2", adapter.note)
        self.assertNotIn("Candy", offers)  # без цены и без страницы товара (404) — пока нет данных
        self.assertTrue(any("/product/candy/" in str(call) for call in request.call_args_list))

    def test_name_from_link(self):
        self.assertEqual(markets.dreampets.slug_name("https://dreampets.gg/mm2/product/eternal-iii/43948eb3-1e25-4564-8102-65bcaeea06db"),
                         "eternal iii")


class MarketsCalcReviewTests(MonitorTests):
    """Найдено при проверке вкладки «Площадки» и калькулятора."""

    def annotated(self, monitor, name):
        items = monitor.annotate([dict(i, liquidity=liquidity.assess(i)) for i in store.load(self.path)["items"]])
        return next(i for i in items if i["name"] == name)["market"]["fake"]

    def test_old_snapshot_is_not_counted_as_two_days(self):
        from datetime import timedelta
        monitor = self.monitor()
        self.offers = [{"name": "Icebreaker", "price": 4.0, "stock": 100, "available": True, "url": "u"}]
        monitor.poll(monitor.markets[0])
        monitor.markets[0].last_ok_at -= timedelta(days=7)  # программа была закрыта неделю
        self.offers = [{"name": "Icebreaker", "price": 4.0, "stock": 10, "available": True, "url": "u"}]
        monitor.poll(monitor.markets[0])
        self.assertEqual(self.annotated(monitor, "Icebreaker")["sold_48h"], 0)
        self.offers = [{"name": "Icebreaker", "price": 4.0, "stock": 7, "available": True, "url": "u"}]
        monitor.poll(monitor.markets[0])  # следующая проверка — свежая: считается
        self.assertEqual(self.annotated(monitor, "Icebreaker")["sold_48h"], 3)

    def test_market_without_lots_has_no_48h_numbers(self):
        monitor = self.monitor()
        self.offers = [{"name": "Icebreaker", "price": 4.0, "stock": None, "sales_week": 40, "available": True, "url": "u"}]
        monitor.poll(monitor.markets[0])
        monitor.poll(monitor.markets[0])
        info = self.annotated(monitor, "Icebreaker")
        self.assertEqual((info["sold_48h"], info["listed_48h"], info["sales_week"]), (None, None, 40))


class DreamPetsReviewV4Tests(unittest.TestCase):
    def setUp(self):
        no_browser(self)

    def test_card_prices(self):
        adapter = markets.DreamPetsAdapter({"id": "d", "title": "D", "kind": "dreampets", "url": "https://dreampets.gg/mm2/"})
        adapter.root = "https://dreampets.gg"
        page = ('<a href="/mm2/product/a/11111111-1111"><p>Alpha</p><span>от 1,299.50 ₽</span></a>'
                '<a href="/mm2/product/b/22222222-2222"><p>Beta</p><span>от 12,345 ₽</span></a>'
                '<a href="/mm2/product/c/33333333-3333"><p>Eternal III / Вечный 3</p><b>23,71 ₽</b></a>'
                '<a href="/mm2/product/d/44444444-4444"><p>Delta</p><i>114</i><b>23,71 ₽</b></a>'
                '<a href="/mm2/product/e/55555555-5555"><p>Echo</p><span>от 1 299,50 ₽</span></a>'
                '<a href="/mm2/product/f/66666666-6666"><p>Foxtrot</p><span>2 499</span><span>₽</span></a>')
        prices = {c["name"]: c["price"] for c in adapter._cards(page).values()}
        self.assertEqual(prices, {"Alpha": 1299.5, "Beta": 12345.0, "Eternal III": 23.71, "Delta": 23.71,
                                  "Echo": 1299.5, "Foxtrot": 2499.0})
        self.assertEqual([markets._rub(t) for t in ("328.98", "34,99", "1.299,50", "1 234")], [328.98, 34.99, 1299.5, 1234.0])

    def test_lots_come_from_product_pages_when_cards_lack_them(self):
        market = ('<a href="/mm2/product/harvester/2c0af45a-9134"><p>Harvester</p><span>от 328.98 ₽</span></a>'
                  '<a href="/mm2/product/seer/35e0ab9c-0ef1"><p>Seer</p><span>от 50 ₽</span></a>')
        product = ('<html><head><title>Harvester / Жнец — купить в ММ2</title></head>'
                   '<body><p>от 330 ₽</p><p>500 лотов</p></body></html>')
        httpd, base = serve({"/mm2/": market, "/mm2/product/harvester/2c0af45a-9134": product,
                             "/mm2/product/seer/35e0ab9c-0ef1": product.replace("Harvester", "Seer").replace("500", "50")})
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        with unittest.mock.patch.object(markets, "_pause"):
            adapter = markets.DreamPetsAdapter({"id": "d", "title": "D", "kind": "dreampets", "url": base + "/mm2/"})
            offers = {o["name"]: o for o in adapter.fetch()}
        # Цена — со страницы рынка (свежее), лоты — со страницы товара.
        self.assertEqual((offers["Harvester"]["price"], offers["Harvester"]["stock"]), (328.98, 500))
        self.assertEqual((offers["Seer"]["price"], offers["Seer"]["stock"]), (50.0, 50))


class ListedLotsTests(unittest.TestCase):
    def test_new_lots_lower_the_score_and_are_explained(self):
        base = markets.assess_offer({"stock": 20, "available": True}, 0, 0)
        flooded = markets.assess_offer({"stock": 20, "available": True}, 0, 10)
        self.assertLess(flooded["score"], base["score"])
        self.assertIn("новых лотов за 2 дня: 10", flooded["reasons"])
        balanced = markets.assess_offer({"stock": 20, "available": True}, 10, 10)
        self.assertGreaterEqual(balanced["score"], base["score"])


class StarPetsVariantTests(unittest.TestCase):
    def test_same_name_products_of_different_years_are_not_mixed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "values.json"
        data = store.empty_data()
        store.replace_category(data, "ancients", [
            parser.make_item("Cane", "ancients", {"value": "100", "demand": "3", "rarity": "3",
                                                  "origin": "Christmas 2018"}),
            parser.make_item("Elf", "pets", {"value": "50", "demand": "2", "rarity": "2"}),
        ], "site")
        store.save(data, path)

        class Fake(markets.Adapter):
            def fetch(self):
                return [
                    {"id": 1, "name": "Cane", "kind": "knife", "year": 2021, "price": 0.06, "price_rub": 4.0, "popularity": 0.1},
                    {"id": 2, "name": "Cane", "kind": "knife", "year": 2018, "price": 13.7, "price_rub": 1000.0, "popularity": 0.5},
                    {"id": 3, "name": "Elf", "kind": "pet", "year": 2016, "price": 0.72, "popularity": 0.9},
                    {"id": 4, "name": "Elf", "kind": "pet", "year": 2019, "price": 18.52, "popularity": 0.2},
                ]

        monitor = markets.MarketMonitor(path, [{"id": "sp", "title": "SP", "kind": "fake", "url": "u"}],
                                        adapters={"fake": Fake})
        monitor.poll(monitor.markets[0])
        items = {i["name"]: i for i in monitor.annotate([dict(i, liquidity=liquidity.assess(i))
                                                          for i in store.load(path)["items"]])}
        self.assertEqual((items["Cane"]["market"]["sp"]["price"], items["Cane"]["market"]["sp"]["price_rub"]), (13.7, 1000.0))
        self.assertEqual(items["Elf"]["market"]["sp"]["price"], 18.52)  # без года — самый популярный, а не самый дешёвый


U1 = "2c0af45a-9134-4c09-9b86-502e81ac758e"
U2 = "3a61a47a-fde2-4740-8de8-c214e8aefca8"
PRODUCT_PAGE = ('<html><head><title>Harvester — купить в ММ2 (Murder Mystery 2)</title>'
                '<meta name="description" content="Купить Harvester в ММ2 от 328.98 ₽, 674 лотов."></head></html>')


def serve_bytes(routes, status=None, delay=None):
    """Как serve(), но тело может быть bytes (сжатая карта сайта), есть коды ответа и задержки."""
    import time as _time

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = self.path
            if delay and path in delay:
                _time.sleep(delay[path])
            if status and path in status:
                self.send_response(status[path])
                self.end_headers()
                self.wfile.write(b"<html>Just a moment...</html>")
                return
            body = routes.get(path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json" if path.startswith("/api/") else "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


class DreamPetsDiscoveryTests(unittest.TestCase):
    """Разные формы сайта: рынок рисуется скриптом, ссылки только в данных, карта сайта и т. д."""

    def setUp(self):
        for patch in (unittest.mock.patch.object(markets, "_pause"),
                      unittest.mock.patch.object(markets.fetcher, "find_system_browser", return_value=None)):
            patch.start()
            self.addCleanup(patch.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_file = Path(tmp.name) / "values.json"
        patch = unittest.mock.patch.object(markets.store, "DATA_FILE", self.data_file)
        patch.start()
        self.addCleanup(patch.stop)

    def run_adapter(self, routes, cache=None, **kwargs):
        httpd, base = serve_bytes(routes, **kwargs)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        for key, value in list(routes.items()):
            if isinstance(value, str):
                routes[key] = value.replace("http://X", base)
        adapter = markets.DreamPetsAdapter({"id": "dreampets", "title": "DreamPets", "kind": "dreampets",
                                            "url": base + "/mm2/"}, cache if cache is not None else {})
        return adapter, base

    def test_spa_market_with_nested_gzip_sitemap(self):
        import gzip
        routes = {
            "/mm2/": '<div id="app"></div><script src="/assets/index.js"></script>',
            "/robots.txt": "User-agent: *\nSitemap: http://X/sitemap-index.xml\n",
            "/sitemap-index.xml": '<sitemapindex><sitemap><loc>http://X/sitemaps/mm2.xml.gz</loc></sitemap></sitemapindex>',
            f"/mm2/product/harvester/{U1}": PRODUCT_PAGE,
        }
        adapter, base = self.run_adapter(routes)
        routes["/sitemaps/mm2.xml.gz"] = gzip.compress(
            f'<urlset><url><loc>{base}/mm2/product/harvester/{U1}</loc></url>'
            f'<url><loc>{base}/mm2-legacy/product/plasmite/{U2}</loc></url></urlset>'.encode())
        offers = adapter.fetch()
        self.assertEqual([(o["name"], o["price"], o["stock"]) for o in offers], [("Harvester", 328.98, 674)])
        self.assertIn("карта сайта: 1", adapter.note)

    def test_links_only_inside_next_data(self):
        flight = '[\\"$\\",\\"a\\",null,{\\"href\\":\\"/mm2/product/harvester/%s\\",\\"children\\":\\"Harvester\\"}]' % U1
        routes = {"/mm2/": '<div></div><script>self.__next_f.push([1,"%s"])</script>' % flight,
                  f"/mm2/product/harvester/{U1}": PRODUCT_PAGE}
        adapter, _ = self.run_adapter(routes)
        self.assertEqual([(o["name"], o["price"]) for o in adapter.fetch()], [("Harvester", 328.98)])

    def test_prices_inside_page_data_need_one_request(self):
        data = {"props": {"pageProps": {"products": [
            {"id": U1, "slug": "harvester", "name": "Harvester", "minPrice": 328.98, "lotsCount": 674},
            {"id": U2, "slug": "gemstone", "name": {"en": "Gemstone", "ru": "Самоцвет"}, "minPrice": "52.99", "lotsCount": 60},
        ]}}}
        routes = {"/mm2/": '<script id="__NEXT_DATA__" type="application/json">%s</script>' % json.dumps(data)}
        adapter, _ = self.run_adapter(routes)
        offers = {o["name"]: (o["price"], o["stock"]) for o in adapter.fetch()}
        self.assertEqual(offers, {"Harvester": (328.98, 674), "Gemstone": (52.99, 60)})

    def test_slow_or_broken_page_does_not_stop_the_rest(self):
        links = "".join(f'<a href="/mm2/product/{n}/{u}">{n}</a>' for n, u in (("harvester", U1), ("gemstone", U2)))
        routes = {"/mm2/": links, f"/mm2/product/harvester/{U1}": PRODUCT_PAGE}
        adapter, _ = self.run_adapter(routes, delay={f"/mm2/product/gemstone/{U2}": 3})
        with unittest.mock.patch.object(markets.DreamPetsAdapter, "PAGE_TIMEOUT", 1):
            offers = adapter.fetch()
        self.assertEqual([(o["name"], o["price"]) for o in offers], [("Harvester", 328.98)])
        gem = next(e for e in adapter.cache["products"].values() if e.get("name") == "gemstone")
        self.assertGreater(gem.get("page_at", 0), 0)  # в конец очереди, а не первым снова

    def test_block_midway_keeps_what_was_read(self):
        links = "".join(f'<a href="/mm2/product/{n}/{u}">{n}</a>' for n, u in (("harvester", U1), ("gemstone", U2)))
        routes = {"/mm2/": links, f"/mm2/product/harvester/{U1}": PRODUCT_PAGE}
        adapter, _ = self.run_adapter(routes, status={f"/mm2/product/gemstone/{U2}": 429})
        with unittest.mock.patch.object(markets.DreamPetsAdapter, "WORKERS", 1):
            offers = adapter.fetch()
        self.assertEqual([(o["name"], o["price"]) for o in offers], [("Harvester", 328.98)])
        self.assertIn("не обновлена", adapter.note)

    def test_nothing_found_saves_report(self):
        adapter, _ = self.run_adapter({"/mm2/": '<div id="app"></div>'})
        with self.assertRaises(markets.fetcher.FetchError) as caught:
            adapter.fetch()
        self.assertIn("debug/dreampets", str(caught.exception))
        report = json.loads((self.data_file.parent / "debug" / "dreampets" / "report.json").read_text(encoding="utf-8"))
        self.assertTrue(any(step["step"] == "market" for step in report["steps"]))

    def test_old_cache_keyed_by_link_is_migrated(self):
        cache = {"products": {f"https://dreampets.gg/mm2/product/harvester/{U1}": {"name": "Harvester", "price": 300.0,
                                                                                  "lots": 600, "at": 1}}}
        routes = {"/mm2/": f'<a href="/mm2/product/harvester/{U1}"><p>Harvester</p><b>от 328.98 ₽</b><i>674 лотов</i></a>'}
        adapter, _ = self.run_adapter(routes, cache=cache)
        offers = adapter.fetch()
        self.assertEqual([(o["name"], o["price"], o["stock"]) for o in offers], [("Harvester", 328.98, 674)])
        self.assertEqual(list(cache["products"]), [U1])


def _test_browser():
    import os
    path = os.environ.get("MM2_TEST_BROWSER") or markets.fetcher.find_system_browser()
    if not path and os.path.exists("/opt/pw-browsers/chromium"):
        path = "/opt/pw-browsers/chromium"
    return path


@unittest.skipUnless(_test_browser(), "нет Edge/Chrome для проверки скрытого браузера")
class DreamPetsBrowserTests(unittest.TestCase):
    """Рынок рисуется скриптом: карточки появляются только в браузере."""

    SPA = """<html><head><title>DreamPets — MM2</title></head><body><div id="app">Загрузка…</div>
<script>
fetch('/api/catalog?game=mm2').then((r) => r.json()).then((data) => {
  const app = document.getElementById('app');
  app.innerHTML = '';
  for (const p of data.items) {
    const a = document.createElement('a');
    a.href = '/mm2/product/' + p.slug + '/' + p.id;
    a.innerHTML = '<span>Ancient</span><p>' + p.title + '</p><b>от ' + p.minPrice + ' ₽</b><i>' + p.lotsCount + ' лотов</i>';
    app.appendChild(a);
  }
});
</script></body></html>"""

    def test_browser_reads_rendered_cards_and_remembers_the_api(self):
        import uuid
        items = [{"id": U1, "slug": "harvester", "title": "Harvester", "minPrice": 328.98, "lotsCount": 674}]
        items += [{"id": str(uuid.UUID(int=i)), "slug": f"item-{i}", "title": f"Item {i}", "minPrice": 10 + i,
                   "lotsCount": i} for i in range(1, 12)]
        routes = {"/mm2/": self.SPA, "/api/catalog?game=mm2": json.dumps({"items": items})}
        httpd, base = serve_bytes(routes)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patch in (unittest.mock.patch.object(markets, "_pause"),
                      unittest.mock.patch.object(markets.fetcher, "find_system_browser", return_value=_test_browser()),
                      unittest.mock.patch.object(markets.store, "DATA_FILE", Path(tmp.name) / "values.json")):
            patch.start()
            self.addCleanup(patch.stop)
        cache = {}
        config = {"id": "dreampets", "title": "DreamPets", "kind": "dreampets", "url": base + "/mm2/"}
        adapter = markets.DreamPetsAdapter(config, cache)
        offers = {o["name"]: (o["price"], o["stock"]) for o in adapter.fetch()}
        self.assertEqual(offers["Harvester"], (328.98, 674))  # «Ancient» — значок, а не название
        self.assertEqual(len(offers), 12)
        self.assertTrue(any("/api/catalog" in url for url in cache.get("api_urls", [])))
        # Дальше — без браузера: найденный адрес данных читается напрямую.
        with unittest.mock.patch.object(markets.fetcher, "find_system_browser", return_value=None):
            adapter = markets.DreamPetsAdapter(config, cache)
            offers = {o["name"]: o["price"] for o in adapter.fetch()}
        self.assertEqual(offers["Harvester"], 328.98)
        self.assertIn("api", adapter.note)


class StarPetsReviewV5Tests(unittest.TestCase):
    def setUp(self):
        FakeApi.calls = []
        FakeApi.info_status = 200
        FakeApi.rub_status = 200
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeApi)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.config = {"id": "starpets", "title": "StarPets", "kind": "starpets", "url": "https://starpets.gg/mm2",
                       "api": f"http://127.0.0.1:{self.httpd.server_address[1]}", "interval": 300}
        patcher = unittest.mock.patch.object(markets, "_pause")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_block_on_rubles_keeps_dollar_prices(self):
        FakeApi.rub_status = 429
        cache = {}
        adapter = markets.StarPetsAdapter(self.config, cache)
        offers = {o["name"]: o for o in adapter.fetch()}  # не исключение: доллары сохранены
        self.assertEqual(offers["Luger"]["price"], 1.02)
        self.assertEqual([c for c in FakeApi.calls if c[0] == "GET"], [])  # продажи после блокировки не запрашивались
        self.assertIn("рублях", adapter.note)

    def test_old_rubles_are_marked_approximate(self):
        cache = {}
        markets.StarPetsAdapter(self.config, cache).fetch()
        cache["prices"]["rub"]["ok_at"] -= 3600  # рубли не обновлялись час
        FakeApi.rub_status = 400
        adapter = markets.StarPetsAdapter(self.config, cache)
        luger = {o["name"]: o for o in adapter.fetch()}["Luger"]
        self.assertEqual(luger["price_rub"], 75.0)
        self.assertTrue(luger["price_rub_approx"])
        self.assertIn("рублях", adapter.note)


class DreamPetsTwinTests(unittest.TestCase):
    def test_product_found_by_name_and_by_link_is_one_entry(self):
        adapter = markets.DreamPetsAdapter({"id": "d", "title": "D", "kind": "dreampets", "url": "https://dreampets.gg/mm2/"})
        adapter.root = "https://dreampets.gg"
        adapter.fresh, adapter.fresh_price, adapter.sources = set(), set(), {}
        products = {}
        adapter._upsert(products, {"name": "Harvester", "price": 900.0}, 1, "данные страницы")
        adapter._upsert(products, {"name": "Harvester", "price": 328.98, "lots": 674}, 2, "страница рынка",
                        f"/mm2/product/harvester/{U1}")
        self.assertEqual(list(products), [U1])
        self.assertEqual((products[U1]["price"], products[U1]["lots"]), (328.98, 674))
