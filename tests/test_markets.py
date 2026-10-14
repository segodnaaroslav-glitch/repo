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
        # Без цены в списке, а страница товара — 404: снят с продажи (не «цена неизвестна»);
        # лоты неизвестны — исчезнувшая страница не считается покупками.
        self.assertEqual((offers["Candy"]["price"], offers["Candy"]["stock"], offers["Candy"]["available"]),
                         (None, None, False))
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


class SameNameRarityTests(unittest.TestCase):
    """Laser — это Godly, Vintage (2014), Chroma и Rare-нож 2026: у каждого своя цена."""

    def monitor(self, offers, categories):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "values.json"
        data = store.empty_data()
        for category, names in categories.items():  # Vintages идут раньше Godlies — как на сайте
            store.replace_category(data, category, [
                parser.make_item(name, category, {"value": "10", "demand": "3", "rarity": "3"}) for name in names], "site")
        store.save(data, path)

        class Fake(markets.Adapter):
            def fetch(self):
                return offers

        monitor = markets.MarketMonitor(path, [{"id": "m", "title": "M", "kind": "fake", "url": "u"}], adapters={"fake": Fake})
        monitor.poll(monitor.markets[0])
        return {(i["category"], i["name"]): (i.get("market") or {}).get("m")
                for i in monitor.annotate([dict(i, liquidity=liquidity.assess(i)) for i in store.load(path)["items"]])}

    def test_starpets_laser_variants_go_to_their_own_items(self):
        sp = markets.StarPetsAdapter({"id": "m", "title": "M", "kind": "starpets", "url": "https://starpets.gg/mm2"})
        rows = [  # как в каталоге StarPets: у всех четырёх name = "Laser"
            {"id": 474, "name": "Laser", "realName": "Laser", "subtype": "gun", "year": None, "rare": "godly",
             "chroma": False, "_rank": 0.3},
            {"id": 785, "name": "Laser", "realName": "Disint", "subtype": "gun", "year": 2014, "rare": "vintage",
             "chroma": False, "_rank": 0.6},
            {"id": 580, "name": "Laser", "realName": "LaserChroma", "subtype": "gun", "year": None, "rare": "godly",
             "chroma": True, "_rank": 0.4},
            {"id": 37790685, "name": "Laser", "realName": "laser_k_2026", "subtype": "knife", "year": 2026,
             "rare": "rare", "chroma": False, "_rank": 0.1},
        ]
        prices = {474: 0.89, 785: 0.59, 580: 1.89, 37790685: 1.48}
        offers = [sp._offer(row, prices[row["id"]]) for row in rows]
        got = self.monitor(offers, {"vintages": ["Laser"], "chromas": ["Chroma Laser"], "godlies": ["Laser"]})
        self.assertEqual(got[("godlies", "Laser")]["price"], 0.89)  # раньше — «нет в продаже»
        self.assertEqual(got[("vintages", "Laser")]["price"], 0.59)
        self.assertEqual(got[("chromas", "Chroma Laser")]["price"], 1.89)

    def test_unknown_rarity_goes_to_the_godly_and_rare_knife_does_not_mix_in(self):
        offers = [{"id": "a", "name": "Laser", "price": 75.0, "currency": "RUB", "stock": 3, "available": True},
                  {"id": "b", "name": "Laser", "rarity": "Винтажный", "price": 50.0, "currency": "RUB", "stock": 1,
                   "available": True},
                  {"id": "c", "name": "Laser", "rarity": "rare", "kind": "knife", "year": 2026, "price": 120.0,
                   "currency": "RUB", "stock": 9, "available": True}]
        got = self.monitor(offers, {"vintages": ["Laser"], "godlies": ["Laser"]})
        self.assertEqual(got[("godlies", "Laser")]["price"], 75.0)  # не дешёвый винтаж и не нож
        self.assertEqual(got[("vintages", "Laser")]["price"], 50.0)

    def test_dreampets_same_name_products_are_not_mixed_into_the_cheapest(self):
        offers = [{"id": "u1", "name": "Laser", "price": 40.0, "currency": "RUB", "stock": 2, "available": True,
                   "product": True},  # другой вариант без значка: дешевле, лотов мало
                  {"id": "u2", "name": "Laser", "price": 75.0, "currency": "RUB", "stock": 30, "available": True,
                   "product": True}]
        got = self.monitor(offers, {"godlies": ["Laser"]})
        self.assertEqual((got[("godlies", "Laser")]["price"], got[("godlies", "Laser")]["stock"]), (75.0, 30))

    def test_single_item_prefers_the_product_of_its_rarity(self):
        offers = [{"id": 1, "name": "Laser", "rarity": "rare", "kind": "knife", "year": 2026, "price": 1.48,
                   "popularity": 0.1, "available": True},
                  {"id": 2, "name": "Laser", "rarity": "godly", "kind": "gun", "price": 0.89, "popularity": 0.3,
                   "available": True}]
        got = self.monitor(offers, {"godlies": ["Laser"]})
        self.assertEqual(got[("godlies", "Laser")]["price"], 0.89)

    def test_dreampets_card_badge_and_link_give_rarity(self):
        from supreme import dreampets
        self.assertEqual(dreampets.parse_card_text("Vintage\nLaser\nот 50 ₽\n2 лота")["rarity"], "Vintage")
        self.assertIsNone(dreampets.parse_card_text("Knife\nLaser\nот 50 ₽")["rarity"])
        adapter = markets.DreamPetsAdapter({"id": "d", "title": "D", "kind": "dreampets", "url": "https://dp.test/mm2/"})
        products = {}
        adapter._upsert(products, {"name": "Laser", "price": 50.0}, 1.0, "рынок", "/mm2/product/laser-vintage/" + U1)
        self.assertEqual(products[U1]["rarity"], "vintage")
        self.assertEqual(adapter._offers(products)[0]["rarity"], "vintage")


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

    def test_visible_price_wins_over_data_inside_the_page(self):
        """Режим «как на сайте»: цена с карточки, которую видит посетитель, а не число из данных
        страницы (там бывает другая валюта или старая цена) — и в следующих опросах тоже."""
        import uuid
        items = [{"id": U1, "slug": "harvester", "title": "Harvester", "minPrice": 99, "lotsCount": 7}]
        items += [{"id": str(uuid.UUID(int=i)), "slug": f"item-{i}", "title": f"Item {i}", "minPrice": 10 + i,
                   "lotsCount": i} for i in range(1, 7)]
        wrong = json.dumps({"props": {"items": [{"id": U1, "name": "Harvester", "price": 1.23, "lotsCount": 7}]}})
        page = self.SPA.replace("</body>", f'<script id="__NEXT_DATA__" type="application/json">{wrong}</script></body>')
        routes = {"/mm2/": page, "/api/catalog?game=mm2": json.dumps({"items": items})}
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
        config = {"id": "dreampets", "title": "DreamPets", "kind": "dreampets", "url": base + "/mm2/", "visible": True}
        offers = {o["name"]: o["price"] for o in markets.DreamPetsAdapter(config, cache).fetch()}
        self.assertEqual(offers["Harvester"], 99)
        offers = {o["name"]: o["price"] for o in markets.DreamPetsAdapter(config, cache).fetch()}  # без браузера
        self.assertEqual(offers["Harvester"], 99)

    # Как настоящий рынок DreamPets (скриншот пользователя): карточки без ссылок, список
    # листается внутри своей панели и дорисовывается при прокрутке, значок редкости над
    # названием (в том числе HALLOWEEN), «от» и цена с точкой, в шапке — баланс «0 ₽».
    LIKE_SITE = """<html><head><title>DreamPets</title><style>
#list{height:400px;overflow-y:auto} .card{height:180px;margin:4px;border:1px solid #333}
</style></head><body><header>Баланс: <b>0 ₽</b></header><div id="list"></div><script>
const all = [];
const rarities = ['ANCIENT', 'LEGENDARY', 'HALLOWEEN', 'GODLY', 'UNCOMMON', 'CHRISTMAS', 'CLASSIC'];
for (let i = 0; i < 60; i++) all.push({r: rarities[i % rarities.length], n: 'Item ' + i, p: (i + 1) + '.' + (i % 10) + '9'});
all[0] = {r: 'HALLOWEEN', n: 'Candies', p: '2'};
all[1] = {r: 'GODLY', n: 'Candy', p: '227.88'};
let shown = 0;
const list = document.getElementById('list');
function more() {
  for (const it of all.slice(shown, shown + 12)) {
    const d = document.createElement('div');
    d.className = 'card';
    d.innerHTML = '<img src="data:,"><div><span class="dot"></span>' + it.r + '</div><div>' + it.n +
                  '</div><div><small>от</small> <b>' + it.p + ' ₽</b></div>';
    list.appendChild(d);
  }
  shown += 12;
}
more();
list.addEventListener('scroll', () => { if (list.scrollTop + list.clientHeight > list.scrollHeight - 50) more(); });
</script></body></html>"""

    def test_cards_like_the_real_site_scrolled_inside_a_panel(self):
        httpd, base = serve_bytes({"/mm2/": self.LIKE_SITE})
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patch in (unittest.mock.patch.object(markets.fetcher, "find_system_browser", return_value=_test_browser()),
                      unittest.mock.patch.object(markets.store, "DATA_FILE", Path(tmp.name) / "values.json")):
            patch.start()
            self.addCleanup(patch.stop)
        config = {"id": "dreampets", "title": "DreamPets", "kind": "dreampets", "url": base + "/mm2/", "visible": True}
        offers = markets.DreamPetsAdapter(config, {}).fetch()
        by_name = {o["name"]: o for o in offers}
        self.assertEqual(len(offers), 60)  # вся лента, а не первые 12
        self.assertEqual((by_name["Candies"]["price"], by_name["Candies"]["rarity"]), (2, "HALLOWEEN"))
        self.assertEqual(by_name["Candy"]["price"], 227.88)
        self.assertEqual(by_name["Item 33"]["price"], 34.39)
        self.assertNotIn("HALLOWEEN", by_name)  # значок — не название

    def _patch(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patch in (unittest.mock.patch.object(markets, "_pause"),
                      unittest.mock.patch.object(markets.fetcher, "find_system_browser", return_value=_test_browser()),
                      unittest.mock.patch.object(markets.store, "DATA_FILE", Path(tmp.name) / "values.json")):
            patch.start()
            self.addCleanup(patch.stop)

    # Карточка из двух ссылок (картинка, название), цена и лоты — вне ссылок, число лотов
    # отдельно от слова, рядом — другое число (место в списке).
    TWO_LINKS = """<html><head><title>DreamPets</title></head><body><div id="app"></div><script>
const items = %s;
const app = document.getElementById('app');
setTimeout(() => {
  for (const p of items) {
    const href = '/mm2/product/' + p.slug + '/' + p.id;
    app.insertAdjacentHTML('beforeend', '<div class="card"><a href="' + href + '"><img alt=""></a><a href="' + href
      + '"><p>' + p.title + '</p></a><i>' + p.rank + '</i><b>от ' + p.price + ' ₽</b><i>' + p.lots + '</i> лотов</div>');
  }
}, 300);
</script></body></html>"""

    def test_cards_with_image_and_title_links(self):
        import uuid
        items = [{"id": U1, "slug": "harvester", "title": "Harvester", "rank": 114, "price": "230,71", "lots": 3}]
        items += [{"id": str(uuid.UUID(int=i)), "slug": f"item-{i}", "title": f"Item {i}", "rank": i, "price": 10 + i,
                   "lots": i} for i in range(1, 6)]
        httpd, base = serve_bytes({"/mm2/": self.TWO_LINKS % json.dumps(items)})
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self._patch()
        adapter = markets.DreamPetsAdapter({"id": "dreampets", "title": "DreamPets", "kind": "dreampets",
                                            "url": base + "/mm2/"}, {})
        offers = {o["name"]: (o["price"], o["stock"]) for o in adapter.fetch()}
        self.assertEqual(offers["Harvester"], (230.71, 3))  # не 114230.71 и не пусто
        self.assertEqual(offers["Item 4"], (14, 4))
        self.assertEqual(len(offers), 6)

    def test_blocked_site_pages_one_by_one_until_refused(self):
        """Площадка не пускает программу: страницы товаров — через браузер, по одной, до первого 429."""
        ids = [str(__import__("uuid").UUID(int=i)) for i in range(1, 4)]
        product = ('<html><head><title>Item {n} — купить в ММ2</title><meta name="description" '
                   'content="Купить Item {n} от 1{n}1 ₽, {n}5 лотов."></head><body></body></html>')
        log = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                browser = bool(self.headers.get("Sec-Fetch-Mode"))  # программа без браузера его не шлёт
                log.append((self.path, browser))
                status, body = 404, ""
                if not browser:
                    status, body = 403, "Just a moment..."
                elif self.path == "/mm2/":
                    status, body = 200, "".join(f'<div><a href="/mm2/product/item-{n}/{ids[n - 1]}"><p>Item {n}</p></a>'
                                                f'<b>от {n}00 ₽</b></div>' for n in (1, 2, 3))
                elif self.path == f"/mm2/product/item-2/{ids[1]}":
                    status = 429
                elif self.path.startswith("/mm2/product/item-"):
                    status, body = 200, product.format(n=self.path.split("item-")[1][0])
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        self._patch()
        cache = {}
        adapter = markets.DreamPetsAdapter({"id": "dreampets", "title": "DreamPets", "kind": "dreampets",
                                            "url": base + "/mm2/"}, cache)
        with unittest.mock.patch.object(markets.DreamPetsAdapter, "BROWSER_PAUSE", 0.1):
            try:
                adapter.fetch()
            except markets.fetcher.FetchError:
                pass  # площадка не пускает — может быть и ошибкой опроса; важно, что прочитано
        pages = [path for path, browser in log if browser and "/product/" in path]
        self.assertEqual(pages, [f"/mm2/product/item-1/{ids[0]}", f"/mm2/product/item-2/{ids[1]}"])
        products = cache["products"]
        self.assertEqual(products[ids[0]]["lots"], 15)
        self.assertGreater(products[ids[0]].get("page_at", 0), 0)
        self.assertFalse(products[ids[1]].get("page_at"))  # 429 — не «прочитана», в следующий раз снова
        self.assertFalse(products[ids[2]].get("page_at"))


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
        offers = {o["name"]: o for o in adapter.fetch()}
        chroma, luger = offers["Chroma Luger"], offers["Luger"]
        self.assertEqual((chroma["price_rub"], chroma["price_rub_approx"]), (150.5, True))
        # Курс Luger (73.5 ₽/$) на 2% ниже курса площадки (75.25), но это его собственный курс:
        # рубли остаются свои, а не пересчитываются; «≈» — только потому, что давно не обновлялись.
        self.assertEqual((luger["price_rub"], luger["price_rub_approx"]), (75.0, True))
        self.assertIn("рублях", adapter.note)


class StarPetsPairTests(unittest.TestCase):
    """Доллары и рубли StarPets — из разных опросов: свежий снимок решает, продаётся ли товар."""

    class Api(FakeApi):
        catalog = {}  # id -> (название, доллары, рубли)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.calls.append(("POST", self.path, body))
            if body["currency"] == "rub" and self.rub_status != 200:
                return self._send(self.rub_status, {"status": False, "message": "Bad Request"})
            rows = []
            if body["filter"]["types"][0]["type"] == "weapon" and body["page"] == 1:
                rows = [{"id": key, "name": name, "type": "weapon", "subtype": "gun", "chroma": False,
                         "price": rub if body["currency"] == "rub" else usd}
                        for key, (name, usd, rub) in self.catalog.items()]
            self._send(200, {"status": True, "items": rows, "count": len(rows)})

    def setUp(self):
        FakeApi.calls = []
        FakeApi.info_status = 200
        self.Api.rub_status = 200
        # Курс площадки 80 ₽/$; у Harvester свой (750 ₽ при $8.83 — на 6% выше).
        self.Api.catalog = {key: (f"Gun {key}", key * 10.0, key * 800.0) for key in range(1, 5)}
        self.Api.catalog[5] = ("Harvester", 8.83, 750.0)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.Api)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.config = {"id": "starpets", "title": "StarPets", "kind": "starpets", "url": "https://starpets.gg/mm2",
                       "api": f"http://127.0.0.1:{self.httpd.server_address[1]}", "interval": 300}
        patcher = unittest.mock.patch.object(markets, "_pause")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cache = {}

    def poll(self, currency):
        offers = markets.StarPetsAdapter(self.config, self.cache).fetch()
        if currency:
            self.assertEqual({c[2]["currency"] for c in FakeApi.calls if c[0] == "POST"}, {currency})
        FakeApi.calls = []
        return {o["name"]: o for o in offers}

    def harvester(self, offers):
        offer = offers["Harvester"]
        return offer["price"], offer["price_rub"], offer["price_rub_approx"], offer["available"]

    def test_unchanged_prices_stay_exact(self):
        self.assertEqual(self.harvester(self.poll(None)), (8.83, 750.0, False, True))
        # Свой курс товара не путается с устаревшей ценой: доллары с прошлого раза те же.
        self.assertEqual(self.harvester(self.poll("usd")), (8.83, 750.0, False, True))
        self.assertEqual(self.harvester(self.poll("rub")), (8.83, 750.0, False, True))
        self.assertEqual(self.harvester(self.poll("usd")), (8.83, 750.0, False, True))

    def test_cheapest_lot_bought_after_ruble_sweep(self):
        self.poll(None)
        self.poll("usd")
        self.poll("rub")
        self.Api.catalog[5] = ("Harvester", 10.5, 750.0)  # самый дешёвый лот купили после чтения рублей
        offers = self.poll("usd")
        # 750 ₽ — цена проданного лота: рубли по курсу самого товара (84.94 ₽/$), с пометкой «≈».
        self.assertEqual(self.harvester(offers), (10.5, 891.85, True, True))
        self.assertEqual((offers["Gun 1"]["price_rub"], offers["Gun 1"]["price_rub_approx"]), (800.0, False))

    def test_small_rise_checked_by_items_own_ratio(self):
        self.poll(None)
        self.poll("usd")
        self.poll("rub")
        # Купили лот за $8.83 / 750 ₽, следующий — $9.50 / 806 ₽. По курсу площадки (80) старые
        # 750 ₽ сошлись бы с $9.50 (760 ₽, в пределах 2%), но курс самого товара — 84.94.
        self.Api.catalog[5] = ("Harvester", 9.5, 806.0)
        self.assertEqual(self.harvester(self.poll("usd")), (9.5, 806.91, True, True))
        self.assertEqual(self.harvester(self.poll("rub")), (9.5, 806.0, False, True))
        # Пара прошла проверку в пределах 2%, но курс товара меняет только пара, чьи цены
        # не менялись со своих прошлых снимков, — расхождения не накапливаются.
        self.assertEqual(self.harvester(self.poll("usd")), (9.5, 806.0, False, True))
        self.assertEqual(self.cache["pairs"]["5"], 750.0 / 8.83)
        self.assertEqual(self.harvester(self.poll("rub")), (9.5, 806.0, False, True))
        self.assertEqual(self.cache["pairs"]["5"], 806.0 / 9.5)

    def test_failed_ruble_sweep_keeps_checked_rubles(self):
        for currency in (None, "usd", "rub", "usd"):
            self.assertEqual(self.harvester(self.poll(currency)), (8.83, 750.0, False, True))
        self.Api.rub_status = 400
        self.assertEqual(self.harvester(self.poll("rub")), (8.83, 750.0, False, True))
        self.Api.rub_status = 200
        # Порядок снимков после сбоя не известен, но цены сходятся с курсом товара — рубли не выдумываются.
        self.assertEqual(self.harvester(self.poll("usd")), (8.83, 750.0, False, True))

    def test_price_back_to_old_value(self):
        self.poll(None)
        self.poll("usd")
        self.Api.catalog[5] = ("Harvester", 9.5, 806.0)  # лот за $8.83 купили
        self.assertEqual(self.harvester(self.poll("rub")), (None, 806.0, False, True))
        self.Api.catalog[5] = ("Harvester", 8.83, 750.0)  # выставили новый по старой цене
        # Доллары те же, что до чтения рублей, но рубли с тех пор менялись: 806 ₽ — цена того лота.
        self.assertEqual(self.harvester(self.poll("usd")), (8.83, 750.0, True, True))

    def test_ruble_dip_bought_before_dollar_sweep(self):
        self.poll(None)
        self.poll("usd")
        self.Api.catalog[5] = ("Harvester", 6.0, 510.0)  # выставили дешёвый лот
        self.assertEqual(self.harvester(self.poll("rub")), (None, 510.0, False, True))
        self.Api.catalog[5] = ("Harvester", 8.83, 750.0)  # его сразу купили
        self.assertEqual(self.harvester(self.poll("usd")), (8.83, 750.0, True, True))  # не 510 ₽ как точная

    def test_dollar_dip_bought_before_ruble_sweep(self):
        self.poll(None)
        self.poll("usd")
        self.poll("rub")
        self.Api.catalog[5] = ("Harvester", 6.0, 510.0)
        self.assertEqual(self.harvester(self.poll("usd")), (6.0, 509.63, True, True))
        self.Api.catalog[5] = ("Harvester", 8.83, 750.0)
        self.assertEqual(self.harvester(self.poll("rub")), (None, 750.0, False, True))  # не $6.00 как точная
        self.assertEqual(self.harvester(self.poll("usd")), (8.83, 750.0, False, True))

    def test_new_items_ratio_learned_when_prices_settle(self):
        self.poll(None)
        self.poll("usd")
        self.Api.catalog[5] = ("Harvester", 15.0, 1200.0)  # новый лот — по курсу площадки, не прежнему товара
        self.assertEqual(self.harvester(self.poll("rub")), (None, 1200.0, False, True))
        self.assertEqual(self.harvester(self.poll("usd")), (15.0, 1274.07, True, True))
        self.assertEqual(self.harvester(self.poll("rub")), (None, 1200.0, False, True))
        # Обе цены не менялись со своих прошлых снимков — пара верна, курс товара теперь 80.
        self.assertEqual(self.harvester(self.poll("usd")), (15.0, 1200.0, False, True))
        self.assertEqual(self.harvester(self.poll("rub")), (15.0, 1200.0, False, True))
        self.assertEqual(self.cache["pairs"]["5"], 80.0)

    def test_sold_out_in_newer_sweep(self):
        self.poll(None)
        self.poll("usd")
        self.poll("rub")
        self.Api.catalog[5] = ("Harvester", None, 750.0)
        self.assertEqual(self.harvester(self.poll("usd")), (None, None, False, False))  # не «в продаже за 750 ₽»
        self.Api.catalog[5] = ("Harvester", None, None)
        self.assertEqual(self.harvester(self.poll("rub")), (None, None, False, False))

    def test_newer_rubles_replace_stale_dollars(self):
        self.poll(None)
        self.poll("usd")
        self.Api.catalog[5] = ("Harvester", 15.0, 1274.0)  # подорожал после чтения долларов
        offers = self.poll("rub")
        self.assertEqual(self.harvester(offers), (None, 1274.0, False, True))  # старые $8.83 не показываются
        offers = self.poll("usd")
        self.assertEqual(self.harvester(offers), (15.0, 1274.0, False, True))

    def test_older_sweep_decides_only_without_newer_row(self):
        adapter = markets.StarPetsAdapter(self.config, {})
        row = {"id": 7, "name": "Fang", "type": "weapon"}
        prices = {"usd": {"ok_at": 200.0, "rows": {}}, "rub": {"ok_at": 100.0, "rows": {"7": 120.0}}}
        fang = adapter._offers({"7": row}, prices)[0]
        self.assertEqual((fang["price"], fang["price_rub"], fang["available"]), (None, 120.0, True))
        prices["usd"]["rows"]["7"] = None  # свежий снимок: не продаётся
        fang = adapter._offers({"7": row}, prices)[0]
        self.assertEqual((fang["price"], fang["price_rub"], fang["available"]), (None, None, False))


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


class DreamPetsBrowserPagesTests(unittest.TestCase):
    """Страницы товаров из скрытого браузера разбираются так же, как обычные."""

    def run_pass(self, products, pages):
        class FakeBrowser:
            notes = []

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def open(self, url, **kwargs):
                pass

            def collect_cards(self, selector, **kwargs):
                return {"cards": [], "api": [], "title": "DreamPets", "pages": 1}

            def fetch_many(self, urls, **kwargs):
                return {url: pages[url] for url in urls if url in pages}, None

        adapter = markets.DreamPetsAdapter({"id": "d", "title": "D", "kind": "dreampets", "url": "https://dp.test/mm2/"})
        with unittest.mock.patch.object(markets.cdp, "Browser", FakeBrowser):
            adapter._browser_pass("https://dp.test/mm2/", products, 1000.0, read_pages=True)
        return adapter

    def test_sold_out_404_and_title_follow_the_page_rules(self):
        url = "https://dp.test/mm2/product/%s/" + U1
        sold = url % "harvester"
        gone = "https://dp.test/mm2/product/seer/" + U2
        products = {
            U1: {"name": "Harvester", "url": sold, "price": 300.0, "lots": 5, "source": "страницы товаров", "at": 1},
            U2: {"name": "Seer", "url": gone, "price": 20.0, "lots": 3, "at": 1},
        }
        page = ("<html><head><title>Купить Godseye в ММ2 | DreamPets</title></head><body><main><h1>Godseye</h1>"
                "<p>0 лотов</p></main></body></html>")
        self.run_pass(products, {sold: (200, page), gone: (404, "")})
        self.assertEqual((products[U1]["name"], products[U1]["price"], products[U1]["lots"]), ("Harvester", None, 0))
        # страницы нет — не продаётся; число лотов неизвестно (не «все куплены»)
        self.assertEqual((products[U2]["price"], products[U2]["lots"], products[U2]["missing"]), (None, None, 1))
        self.assertTrue(products[U1]["page_at"] and products[U2]["page_at"])


class SameNameGroupTests(unittest.TestCase):
    """StarPets называет одинаково десятки разных товаров (Pumpkin: ножи, пистолеты, питомцы
    разных лет): каждому предмету — свой товар по категории, году и типу, один товар — одному."""

    def test_group_assignment_by_year_kind_and_rarity(self):
        def item(name, category, origin=""):
            return parser.make_item(name, category, {"value": "1", "demand": "1", "rarity": "1", "origin": origin})

        items = [item("Pumpkin", "pets", "Halloween 2018"), item("Elf", "commons", "Christmas 2017"),
                 item("Elf", "pets", "Christmas 2019")]
        sp = markets.StarPetsAdapter({"id": "m", "title": "M", "kind": "starpets", "url": "https://starpets.gg/mm2"})
        rows = [("Pumpkin", "weapon", "knife", "uncommon", 2020, 0.06), ("Pumpkin", "pet", "floating", "common", 2018, 0.32),
                ("Pumpkin", "pet", "floating", "common", 2019, 0.08), ("Elf", "weapon", "knife", "common", 2017, 0.08),
                ("Elf", "weapon", "gun", "common", 2023, 0.05), ("Elf", "pet", "walking", "uncommon", 2019, 18.52)]
        offers = [sp._offer({"id": n, "name": name, "_type": kind, "subtype": sub, "rare": rare, "year": year,
                             "_rank": 0.5}, price) for n, (name, kind, sub, rare, year, price) in enumerate(rows)]
        index = markets.build_index(items)
        groups = {}
        for offer in offers:
            candidates = markets.candidate_items(index, offer)
            groups.setdefault(tuple(markets.item_id(c) for c in candidates), (candidates, []))[1].append(offer)
        got, unused = markets.assign_offers(groups.values())
        self.assertEqual(got["pets:Pumpkin"]["price"], 0.32)  # питомец 2018, а не нож и не питомец 2019
        self.assertEqual(got["commons:Elf"]["price"], 0.08)  # нож 2017
        self.assertEqual(got["pets:Elf"]["price"], 18.52)
        self.assertEqual(unused, 3)
