import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from supreme import fetcher, liquidity, parser, server, store

CARD = "<div>{name}</div><div>Value - {value}</div><div>Range - [{low} - {high}]</div><div>Demand - {demand}</div>"
HOME = "<p>Values Last Updated - October 5th, 2026 at 12:49 PM // x</p>"


def fake_page(slug):
    return "<html><body>" + CARD.format(name=f"{slug} item", value=110, low=100, high=120, demand=5) + "</body></html>"


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "values.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_update_keeps_old_category_on_error(self):
        old = store.empty_data()
        store.replace_category(old, "godlies", [parser.make_item("Old Godly", "godlies", {"value": "7"})], "site")
        store.save(old, self.path)

        def fetch(url):
            if url.endswith("/godlies"):
                raise fetcher.FetchError("нет сети")
            if url == fetcher.HOME_URL:
                return HOME
            return fake_page(url.rsplit("/", 1)[-1])

        data = store.update_from_site(fetch=fetch, log=lambda line: None, path=self.path)
        names = {item["name"] for item in data["items"]}
        self.assertIn("Old Godly", names)
        self.assertIn("ancients item", names)
        self.assertEqual(data["site_last_updated"], "October 5th, 2026 at 12:49 PM")
        self.assertTrue(data["fetched_at"])
        self.assertEqual(len(data["errors"]), 1)
        ancients = next(item for item in data["items"] if item["category"] == "ancients")
        self.assertEqual(ancients["value"], 100)
        self.assertEqual(store.load(self.path)["items"], data["items"])

    def test_update_fails_when_nothing_loaded(self):
        def fetch(url):
            raise fetcher.FetchError("нет сети")

        with self.assertRaises(store.UpdateError):
            store.update_from_site(fetch=fetch, log=lambda line: None, path=self.path)
        self.assertFalse(self.path.exists())

    def test_import_text(self):
        items = store.import_text("Some Knife\nValue - 1,330\nRange - [1,320 - 1,340]\n", "godlies", self.path)
        self.assertEqual(items[0]["value"], 1320)
        self.assertEqual(store.load(self.path)["items"][0]["name"], "Some Knife")
        with self.assertRaises(ValueError):
            store.import_text("пусто", "godlies", self.path)
        with self.assertRaises(ValueError):
            store.import_text("Some Knife\nValue - 1\n", "nope", self.path)


class LiquidityTests(unittest.TestCase):
    def item(self, **fields):
        base = {"category": "godlies", "demand": 5, "stability": "Stable", "range_text": "", "value": 100}
        base.update(fields)
        return base

    def test_levels(self):
        self.assertEqual(liquidity.assess(self.item(demand=8))["level"], liquidity.LIQUID)
        self.assertEqual(liquidity.assess(self.item(demand=4))["level"], liquidity.MEDIUM)
        self.assertEqual(liquidity.assess(self.item(demand=1))["level"], liquidity.ILLIQUID)
        self.assertEqual(liquidity.assess(self.item(demand=None))["level"], liquidity.ILLIQUID)
        self.assertEqual(liquidity.assess(self.item(category="untradables"))["level"], liquidity.UNTRADABLE)

    def test_stability_and_range_adjust_score(self):
        base = liquidity.assess(self.item())["score"]
        self.assertGreater(liquidity.assess(self.item(stability="Overpaid For"))["score"], base)
        self.assertLess(liquidity.assess(self.item(stability="Receding"))["score"], base)
        self.assertGreater(liquidity.assess(self.item(range_text="100 - 102"))["score"], base)
        self.assertLess(liquidity.assess(self.item(range_text="100 - 150"))["score"], base)

    def test_score_is_clamped(self):
        self.assertEqual(liquidity.assess(self.item(demand=10, stability="Overpaid For"))["score"], 100)
        self.assertEqual(liquidity.assess(self.item(demand=0, stability="Receding"))["score"], 0)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "values.json"
        self.update_calls = 0

        def update(log):
            self.update_calls += 1
            log("готово")

        self.httpd = server.make_server(port=0, data_path=self.path, update=update)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        all_headers = {"Host": f"127.0.0.1:{self.port}"}
        all_headers.update(headers or {})
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode("utf-8")
        connection.request(method, path, body=body, headers=all_headers)
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, payload

    def test_static_files(self):
        for path in ("/", "/app.js", "/style.css"):
            status, body = self.request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertTrue(body)
        self.assertEqual(self.request("GET", "/../mm2_values.py")[0], 404)

    def test_data_empty(self):
        status, body = self.request("GET", "/api/data")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["items"], [])
        self.assertEqual(len(data["categories"]), len(parser.CATEGORIES))

    def test_import_then_data_has_liquidity(self):
        status, body = self.request(
            "POST", "/api/import",
            {"category": "godlies", "text": "Some Knife\nValue - 1,330\nRange - [1,320 - 1,340]\nDemand - 7\n"},
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, body)
        data = json.loads(self.request("GET", "/api/data")[1])
        self.assertEqual(data["items"][0]["value"], 1320)
        self.assertEqual(data["items"][0]["liquidity"]["level"], "liquid")
        godlies = next(c for c in data["categories"] if c["slug"] == "godlies")
        self.assertEqual(godlies["count"], 1)

    def test_import_errors(self):
        json_header = {"Content-Type": "application/json"}
        self.assertEqual(self.request("POST", "/api/import", {"category": "godlies", "text": "x"}, json_header)[0], 400)
        self.assertEqual(self.request("POST", "/api/import", {"text": 1}, json_header)[0], 400)
        self.assertEqual(self.request("POST", "/api/import", b"not json", json_header)[0], 400)
        self.assertEqual(self.request("POST", "/api/import", b"[]", json_header)[0], 400)

    def test_post_protection(self):
        body = {"category": "godlies", "text": "A\nValue - 1\n"}
        self.assertEqual(self.request("POST", "/api/import", body, {"Content-Type": "text/plain"})[0], 415)
        evil = {"Content-Type": "application/json", "Origin": "http://evil.example"}
        self.assertEqual(self.request("POST", "/api/import", body, evil)[0], 403)
        rebinding = {"Content-Type": "application/json", "Host": "evil.example"}
        self.assertEqual(self.request("POST", "/api/import", body, rebinding)[0], 403)
        self.assertEqual(self.request("GET", "/api/data", headers={"Host": "evil.example"})[0], 403)

    def test_update_job(self):
        status, body = self.request("POST", "/api/update", {}, {"Content-Type": "application/json"})
        self.assertEqual(status, 202)
        for _ in range(50):
            state = json.loads(self.request("GET", "/api/status")[1])
            if not state["running"]:
                break
            time.sleep(0.05)
        self.assertFalse(state["running"])
        self.assertIsNone(state["error"])
        self.assertEqual(state["log"], ["готово"])
        self.assertEqual(self.update_calls, 1)


if __name__ == "__main__":
    unittest.main()
