import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from supreme import dpreport, markets
from tests import test_markets as tm
from tests.test_markets import U1, _test_browser, serve_bytes


class DreamPetsReportTests(unittest.TestCase):
    """Отчёт «Проверить DreamPets»: карточки, ответы API площадки и страницы товаров — одним файлом."""

    def test_report_records_cards_api_and_pages(self):
        if not _test_browser():
            self.skipTest("нет Edge/Chrome")
        items = [{"id": U1, "slug": "harvester", "title": "Harvester", "minPrice": 328.98, "lotsCount": 674}]
        page = ('<html><head><title>Harvester</title></head><body><main><h1>Harvester</h1>'
                '<p>от 328,98 ₽</p><p>674 лота</p></main></body></html>')
        routes = {"/mm2/": tm.DreamPetsBrowserTests.SPA, "/api/catalog?game=mm2": json.dumps({"items": items}),
                  f"/mm2/product/harvester/{U1}": page}
        httpd, base = serve_bytes(routes)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with unittest.mock.patch.object(markets.fetcher, "find_system_browser", return_value=_test_browser()):
            path, report = dpreport.make_report(base + "/mm2/", ours={f"godlies:Harvester {U1}": {"price": 300}},
                                                folder=tmp.name, product_pages=1)
        self.assertTrue(Path(path).exists())
        saved = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertEqual(saved["cards"][0]["parsed"]["price"], 328.98)
        api = [a for a in saved["api"] if "/api/catalog" in a["url"]]
        self.assertTrue(api and api[0]["products_found"] == 1 and "minPrice" in api[0]["body"])
        self.assertEqual(saved["pages"][0]["parsed"]["price"], 328.98)
        self.assertIn("674 лота", saved["pages"][0]["text"])
        self.assertTrue(saved["card_html"] and "Harvester" in saved["card_html"][0]["html"])
        self.assertEqual(list(saved["ours"].values())[0], {"price": 300})


class DreamPetsCheckEndpointTests(unittest.TestCase):
    def test_button_endpoint_saves_report_and_answers(self):
        import http.client
        import threading
        from supreme import server
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        calls = []

        def fake(base, ours=None, **kwargs):
            calls.append((base, ours))
            path = Path(tmp.name) / "dreampets-check.json"
            path.write_text("{}", encoding="utf-8")
            return path, {"cards": [1, 2], "api": [1], "pages": [1], "notes": []}

        httpd = server.make_server(port=0, data_path=Path(tmp.name) / "values.json", update=lambda log: None)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        with unittest.mock.patch.object(dpreport, "make_report", fake), \
                unittest.mock.patch.object(server.os, "startfile", create=True) as opened:
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
            conn.request("POST", "/api/dreampets/check", body="{}", headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            body = json.loads(response.read())
        self.assertEqual(response.status, 200, body)
        self.assertEqual((body["cards"], body["api"], body["pages"]), (2, 1, 1))
        self.assertEqual(calls[0][0], "https://dreampets.gg/mm2/")
        self.assertEqual(opened.call_count, 1 if server.os.name == "nt" else 0)  # папка с отчётом — в Windows
