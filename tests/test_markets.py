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
        self.assertEqual(index["batwing"]["category"], "ancients")
        self.assertNotIn("locked", index)


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
        self.assertEqual((info["price"], info["stock"], info["sold_24h"]), (4.5, 5, 3))
        self.assertEqual(ice["combined"]["sources"], 2)
        fang = next(i for i in items if i["name"] == "Fang")
        self.assertEqual(fang["market"], {})
        self.assertEqual(fang["combined"]["sources"], 1)

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
        self.assertEqual(markets.assess_offer({"stock": 50, "available": False}, 0)["score"], 10)


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


if __name__ == "__main__":
    unittest.main()


class FakeApi(BaseHTTPRequestHandler):
    """StarPets API для тестов."""
    calls = []
    info_status = 200

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
        rows = {
            "weapon": [
                {"id": 485, "name": "Luger", "type": "weapon", "subtype": "gun", "chroma": False, "price": 1.02},
                {"id": 756, "name": "Luger", "type": "weapon", "subtype": "gun", "chroma": True, "price": 2.0},
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
        posts = [c for c in FakeApi.calls if c[0] == "POST"]
        self.assertEqual([p[2]["filter"]["types"][0]["type"] for p in posts], ["weapon", "pet", "misc"])
        gets_before = len([c for c in FakeApi.calls if c[0] == "GET"])
        markets.StarPetsAdapter(self.config, cache).fetch()  # продажи уже свежие — не запрашиваются снова
        self.assertEqual(len([c for c in FakeApi.calls if c[0] == "GET"]), gets_before)

    def test_sales_endpoint_missing_is_switched_off(self):
        FakeApi.info_status = 404
        cache = {}
        for _ in range(3):
            adapter = markets.StarPetsAdapter(self.config, cache)
            offers = adapter.fetch()
        self.assertTrue(cache["info_off"])
        self.assertIn("не отдаёт", adapter.note)
        self.assertTrue(all(o["sales_week"] is None for o in offers))


class DreamPetsTests(unittest.TestCase):
    def setUp(self):
        patcher = unittest.mock.patch.object(markets, "_pause")
        patcher.start()
        self.addCleanup(patcher.stop)

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
        self.assertIn("товаров на рынке: 2", adapter.note)

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
