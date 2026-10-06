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
        self.assertEqual([error["category"] for error in data["errors"]], ["godlies"])
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
        items, _ = store.import_text("Some Knife\nValue - 1,330\nRange - [1,320 - 1,340]\n", "godlies", self.path)
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


class StoreRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "values.json"
        self.quiet = lambda line: None

    def tearDown(self):
        self.tmp.cleanup()

    def site(self, overrides=None):
        overrides = overrides or {}

        def fetch(url):
            slug = url.rsplit("/", 1)[-1]
            if slug in overrides:
                result = overrides[slug]
                if isinstance(result, Exception):
                    raise result
                return result
            if url == fetcher.HOME_URL:
                return HOME
            return fake_page(slug)
        return fetch

    def test_corrupt_file_is_set_aside_on_update_and_import(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            store.load(self.path)
        store.import_text("Alpha\nValue - 5\n", "rares", self.path)
        self.assertEqual(store.load(self.path)["items"][0]["name"], "Alpha")
        self.assertTrue(list(Path(self.tmp.name).glob("values.broken-*.json")))

    def test_bom_and_bad_items_tolerated(self):
        self.path.write_text('﻿{"items": [{"name": "A", "category": "rares"}, 5, {"x": 1}]}', encoding="utf-8")
        self.assertEqual([item["name"] for item in store.load(self.path)["items"]], ["A"])

    def test_one_broken_page_does_not_stop_update(self):
        data = store.update_from_site(fetch=self.site({"rares": RuntimeError("boom")}), log=self.quiet, path=self.path)
        self.assertEqual([error["category"] for error in data["errors"]], ["rares"])
        self.assertIn("godlies", {item["category"] for item in data["items"]})

    def test_no_network_stops_early(self):
        calls = []

        def fetch(url):
            calls.append(url)
            raise fetcher.NetworkError("нет интернета")
        with self.assertRaises(store.UpdateError) as caught:
            store.update_from_site(fetch=fetch, log=self.quiet, path=self.path)
        self.assertEqual(len(calls), 2)
        self.assertIn("Импорт", str(caught.exception))

    def test_import_during_update_is_kept(self):
        def fetch(url):
            if url.endswith("/commons"):  # пока идёт загрузка, пользователь импортирует Pets
                store.import_text("My Pet\nValue - 7\n", "pets", self.path)
            return self.site({"pets": fetcher.FetchError("404")})(url)
        data = store.update_from_site(fetch=fetch, log=self.quiet, path=self.path)
        self.assertIn("My Pet", {item["name"] for item in data["items"]})

    def test_suspicious_drop_keeps_old_prices(self):
        old = store.empty_data()
        store.replace_category(old, "godlies", [parser.make_item(f"G{i}", "godlies", {"value": "1"}) for i in range(20)], "site")
        store.save(old, self.path)
        data = store.update_from_site(fetch=self.site(), log=self.quiet, path=self.path)
        godlies = [item for item in data["items"] if item["category"] == "godlies"]
        self.assertEqual(len(godlies), 20)
        self.assertIn("было 20", data["errors"][0]["message"])

    def test_import_clears_category_error_and_saves_date_only(self):
        store.update_from_site(fetch=self.site({"rares": fetcher.FetchError("404")}), log=self.quiet, path=self.path)
        store.import_text("Alpha\nValue - 5\n", "rares", self.path)
        self.assertEqual(store.load(self.path)["errors"], [])
        items, date = store.import_text("Values Last Updated - October 6th, 2026 at 1:00 PM", "rares", self.path)
        self.assertEqual((items, date), ([], "October 6th, 2026 at 1:00 PM"))
        data = store.load(self.path)
        self.assertEqual(data["site_last_updated"], "October 6th, 2026 at 1:00 PM")
        self.assertEqual([item["name"] for item in data["items"] if item["category"] == "rares"], ["Alpha"])


class ServerRegressionTests(ServerTests):
    def test_corrupt_file_still_gives_categories(self):
        self.path.write_text("garbage", encoding="utf-8")
        status, body = self.request("GET", "/api/data")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(data["load_error"])
        self.assertEqual(len(data["categories"]), len(parser.CATEGORIES))

    def test_category_dates_and_weights_in_payload(self):
        store.import_text("Alpha\nValue - 5\n", "rares", self.path)
        data = json.loads(self.request("GET", "/api/data")[1])
        rares = next(c for c in data["categories"] if c["slug"] == "rares")
        self.assertEqual(rares["source"], "import")
        self.assertTrue(rares["updated_at"])
        self.assertEqual(data["liquidity"]["stability"]["receding"], -15)

    def test_save_error_reported(self):
        from unittest import mock
        with mock.patch.object(store, "save", side_effect=PermissionError("занято")):
            status, body = self.request("POST", "/api/import", {"category": "rares", "text": "Alpha\nValue - 1\n"},
                                        {"Content-Type": "application/json"})
        self.assertEqual(status, 500)
        self.assertIn("занято", json.loads(body)["error"])

    def test_host_without_port_rejected(self):
        self.assertEqual(self.request("GET", "/api/data", headers={"Host": "localhost"})[0], 403)
        self.assertEqual(self.request("GET", "/api/data", headers={"Host": f"localhost:{self.port}"})[0], 200)


class CliTests(unittest.TestCase):
    def test_read_text_file_encodings(self):
        import mm2_values
        with tempfile.TemporaryDirectory() as tmp:
            for encoding in ("utf-8", "utf-8-sig", "utf-16", "cp1251"):
                path = Path(tmp) / f"{encoding}.txt"
                path.write_bytes("Нож Alpha\nValue - 5\n".encode(encoding))
                self.assertEqual(mm2_values.read_text_file(path), "Нож Alpha\nValue - 5\n", encoding)


class SecondReviewStoreTests(StoreRegressionTests):
    def test_file_from_previous_version(self):
        self.path.write_text(json.dumps({
            "items": [{"name": "Old", "category": "rares", "value": "12", "demand": True}],
            "errors": ["Godlies: предметы не найдены", "Unknown: x", 5],
            "categories": {"rares": "bad", "commons": {"updated_at": "2026-01-01T00:00:00+00:00"}},
        }), encoding="utf-8")
        data = store.load(self.path)
        self.assertEqual((data["items"][0]["value"], data["items"][0]["demand"], data["items"][0]["value_text"]), (None, None, ""))
        self.assertEqual(data["errors"], [{"category": "godlies", "title": "Godlies", "message": "предметы не найдены"}])
        self.assertEqual(list(data["categories"]), ["commons"])
        store.import_text("Alpha\nValue - 5\n", "godlies", self.path)
        self.assertEqual(store.load(self.path)["errors"], [])

    def test_read_error_never_overwrites_prices(self):
        from unittest import mock
        store.import_text("Alpha\nValue - 5\n", "rares", self.path)
        before = self.path.read_bytes()
        with mock.patch.object(store, "load", side_effect=PermissionError("занято")), \
                mock.patch.object(store.time, "sleep"):
            with self.assertRaises(PermissionError):
                store.import_text("Beta\nValue - 6\n", "commons", self.path)
            with self.assertRaises(PermissionError):
                store.update_from_site(fetch=self.site(), log=self.quiet, path=self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_shrunk_category_accepted_second_time_or_after_import(self):
        old = store.empty_data()
        store.replace_category(old, "godlies", [parser.make_item(f"G{i}", "godlies", {"value": "1"}) for i in range(20)], "site")
        store.save(old, self.path)
        first = store.update_from_site(fetch=self.site(), log=self.quiet, path=self.path)
        self.assertEqual(len([i for i in first["items"] if i["category"] == "godlies"]), 20)
        second = store.update_from_site(fetch=self.site(), log=self.quiet, path=self.path)
        self.assertEqual([i["name"] for i in second["items"] if i["category"] == "godlies"], ["godlies item"])

        imported = store.empty_data()
        store.replace_category(imported, "godlies", [parser.make_item(f"G{i}", "godlies", {"value": "1"}) for i in range(20)], "import")
        store.save(imported, self.path)
        third = store.update_from_site(fetch=self.site(), log=self.quiet, path=self.path)
        self.assertEqual(len([i for i in third["items"] if i["category"] == "godlies"]), 1)

    def test_import_into_downloaded_category_during_update_wins(self):
        def fetch(url):
            if url.endswith("/commons"):
                store.import_text("Values Last Updated - Oct 7th, 2026 at 1:00 PM\nMy Godly\nValue - 7\n", "godlies", self.path)
            return self.site({"godlies": fetcher.FetchError("404")})(url)
        data = store.update_from_site(fetch=fetch, log=self.quiet, path=self.path)
        self.assertEqual([i["name"] for i in data["items"] if i["category"] == "godlies"], ["My Godly"])
        self.assertNotIn("godlies", [e["category"] for e in data["errors"]])
        self.assertEqual(data["site_last_updated"], "Oct 7th, 2026 at 1:00 PM")

    def test_debug_page_saved_next_to_data_file(self):
        store.update_from_site(fetch=self.site({"rares": "<html>empty</html>"}), log=self.quiet, path=self.path)
        self.assertTrue((self.path.parent / "debug" / "rares.html").exists())

    def test_import_with_many_unnamed_cards_rejected(self):
        with self.assertRaises(ValueError):
            store.import_text("Value - 1\nValue - 2\nValue - 3\nAlpha\nValue - 4\n", "rares", self.path)

    def test_popup_page_with_protection_marker_not_blocked(self):
        page = '<script src="/_Incapsula_Resource"></script><script>var _svPopup = {"Mu": {"value": "5"}};</script>'
        self.assertFalse(fetcher.looks_blocked(page))


class SecondReviewCliTests(unittest.TestCase):
    def test_bom_with_bad_byte(self):
        import mm2_values
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.txt"
            path.write_bytes(b"\xef\xbb\xbfAlpha \xff\nValue - 5\n")
            self.assertTrue(mm2_values.read_text_file(path).startswith("Alpha "))

    def test_running_instance_found_on_fallback_port(self):
        import mm2_values
        httpd = server.make_server(port=0, data_path=Path(tempfile.mkdtemp()) / "v.json", update=lambda log: None)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            port = httpd.server_address[1]
            self.assertEqual(mm2_values.running_instance(port - 3, tries=5), f"http://127.0.0.1:{port}/")
        finally:
            httpd.shutdown()
            httpd.server_close()


class AutoSyncServerTests(unittest.TestCase):
    def test_status_has_sync_and_check_endpoint(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        httpd = server.make_server(port=0, data_path=Path(tmp.name) / "v.json", update=lambda log: None,
                                   auto_sync=True, check_site=lambda: None)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        port = httpd.server_address[1]
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/api/status", headers={"Host": f"127.0.0.1:{port}"})
        status = json.loads(connection.getresponse().read())
        self.assertIn("next_check_at", status["sync"])
        self.assertIn("data_version", status)
        connection.request("POST", "/api/check", body=b"{}",
                           headers={"Host": f"127.0.0.1:{port}", "Content-Type": "application/json"})
        self.assertEqual(connection.getresponse().status, 202)
        connection.close()
