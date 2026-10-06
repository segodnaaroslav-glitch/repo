import os
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from supreme import fetcher, paths

# Страница, которая, как защита сайта, сначала ставит cookie и перезагружается,
# а карточку показывает только потом.
CHALLENGE_PAGE = """<html><body><div id="x">stub</div><script>
if (!document.cookie.includes("ok=1")) { document.cookie = "ok=1"; setTimeout(() => location.reload(), 500); }
else { setTimeout(() => { document.getElementById("x").innerHTML = "<div>Demo Blade</div><div>Value - 1,330</div>"; }, 300); }
</script></body></html>"""

BROWSER = os.environ.get("MM2_TEST_BROWSER") or fetcher.find_system_browser() or (
    "/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None
)


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@unittest.skipUnless(BROWSER, "на компьютере нет Edge/Chrome/Chromium")
class SystemBrowserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        Path(self.tmp.name, "page.html").write_text(CHALLENGE_PAGE, encoding="utf-8")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=self.tmp.name))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/page.html"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def test_browser_passes_reload_check(self):
        page = fetcher.SystemBrowserFetcher(BROWSER).fetch(self.url)
        self.assertIn("Value - 1,330", page)

    def test_fetcher_falls_back_to_browser_when_blocked(self):
        blocked = mock.patch.object(fetcher, "fetch_plain", side_effect=fetcher.BlockedError("blocked"))
        no_playwright = mock.patch.object(fetcher.BrowserFetcher, "available", return_value=False)
        system = mock.patch.object(fetcher, "find_system_browser", return_value=BROWSER)
        with blocked as plain, no_playwright, system:
            get = fetcher.Fetcher()
            self.assertIn("Value - 1,330", get(self.url))
            self.assertIn("Value - 1,330", get(self.url))
        self.assertEqual(plain.call_count, 1)  # после блокировки сразу браузер


class FetcherErrorTests(unittest.TestCase):
    def test_no_browser_gives_clear_error(self):
        blocked = mock.patch.object(fetcher, "fetch_plain", side_effect=fetcher.BlockedError("blocked"))
        no_playwright = mock.patch.object(fetcher.BrowserFetcher, "available", return_value=False)
        no_system = mock.patch.object(fetcher, "find_system_browser", return_value=None)
        with blocked, no_playwright, no_system:
            with self.assertRaises(fetcher.BlockedError) as caught:
                fetcher.Fetcher()("https://example.invalid/")
        self.assertIn("Импорт", str(caught.exception))

    def test_blocked_page_detected(self):
        self.assertTrue(fetcher.looks_blocked('<script src="/_Incapsula_Resource?x"></script>'))
        self.assertFalse(fetcher.looks_blocked("<div>Value - 1</div>"))


class PathTests(unittest.TestCase):
    def test_source_layout(self):
        self.assertFalse(paths.FROZEN)
        self.assertTrue((paths.resource_dir() / "web" / "index.html").is_file())

    def test_data_dir_falls_back_when_not_writable(self):
        with TemporaryDirectory() as tmp:
            with mock.patch.object(paths, "_writable", return_value=False), \
                    mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}):
                self.assertEqual(paths.data_dir(), Path(tmp) / "MM2Values" / "data")


if __name__ == "__main__":
    unittest.main()


class StatusBlockTests(unittest.TestCase):
    def serve(self, status, body):
        class Handler(Quiet):
            def do_GET(self):
                payload = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}/"

    def test_block_statuses_raise_blocked_at_once(self):
        for status in (403, 429, 503):
            url = self.serve(status, "<html>Access denied</html>")
            with mock.patch.object(fetcher.time, "sleep") as sleep:
                with self.assertRaises(fetcher.BlockedError):
                    fetcher.fetch_plain(url)
            sleep.assert_not_called()

    def test_page_with_protection_script_but_content_is_not_blocked(self):
        page = '<script src="/_Incapsula_Resource?x"></script><div>Alpha</div><div>Value - 10</div>'
        self.assertFalse(fetcher.looks_blocked(page))
        self.assertEqual(fetcher.fetch_plain(self.serve(200, page)), page)

    def test_server_error_is_not_network_error(self):
        url = self.serve(500, "oops")
        with mock.patch.object(fetcher.time, "sleep"):
            with self.assertRaises(fetcher.FetchError) as caught:
                fetcher.fetch_plain(url)
        self.assertNotIsInstance(caught.exception, fetcher.NetworkError)

    def test_connection_refused_is_network_error(self):
        with mock.patch.object(fetcher.time, "sleep"):
            with self.assertRaises(fetcher.NetworkError):
                fetcher.fetch_plain("http://127.0.0.1:9/", timeout=2)


class FrozenPathTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.exe_dir = Path(tmp.name) / "Загрузки"
        self.old = self.exe_dir / "data"
        self.local = Path(tmp.name) / "Local"
        self.target = self.local / "MM2Values" / "data"
        for patch in (mock.patch.object(paths, "FROZEN", True),
                      mock.patch.object(paths.sys, "executable", str(self.exe_dir / "MM2Values.exe")),
                      mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.local)})):
            patch.start()
            self.addCleanup(patch.stop)

    def make_old(self, files):
        for name, text in files.items():
            path = self.old / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def test_exe_keeps_data_in_localappdata_and_moves_old_folder(self):
        self.make_old({"values.json": "{}", "markets-state.json": "[]", "debug/godlies.html": "x"})
        self.assertEqual(paths.data_dir(), self.old)  # до переноса читаются старые цены
        self.assertFalse(self.local.exists())          # и ничего не создаётся
        folder = paths.migrate_old_data()
        self.assertEqual(folder, self.target)
        self.assertEqual(paths.data_dir(), self.target)
        self.assertEqual((folder / "values.json").read_text(encoding="utf-8"), "{}")
        self.assertTrue((folder / "markets-state.json").exists())
        self.assertTrue((folder / "debug" / "godlies.html").exists())
        self.assertFalse(self.old.exists())  # рядом с exe папки больше нет

    def test_foreign_data_folder_is_left_alone(self):
        self.make_old({"thesis.docx": "my work", "photos/cat.jpg": "meow"})
        self.assertEqual(paths.migrate_old_data(), self.target)
        self.assertTrue((self.old / "thesis.docx").exists())
        self.assertTrue((self.old / "photos" / "cat.jpg").exists())

    def test_only_program_files_are_moved(self):
        self.make_old({"values.json": "{}", "notes.txt": "mine"})
        paths.migrate_old_data()
        self.assertTrue((self.target / "values.json").exists())
        self.assertFalse((self.target / "notes.txt").exists())
        self.assertEqual((self.old / "notes.txt").read_text(encoding="utf-8"), "mine")
        self.assertFalse((self.old / "values.json").exists())

    def test_existing_empty_target_does_not_block_migration(self):
        self.make_old({"values.json": "{}"})
        self.target.mkdir(parents=True)
        self.assertEqual(paths.migrate_old_data(), self.target)
        self.assertTrue((self.target / "values.json").exists())

    def test_failed_copy_keeps_old_folder_and_retries_later(self):
        self.make_old({"values.json": "{}", "markets.json": "[]"})
        with mock.patch.object(paths.shutil, "copy2", side_effect=OSError("disk full")):
            self.assertEqual(paths.migrate_old_data(), self.old)
        self.assertTrue((self.old / "values.json").exists())
        self.assertFalse((self.target / "values.json").exists())
        self.assertEqual(paths.data_dir(), self.old)  # следующий запуск — снова старая папка
        self.assertEqual(paths.migrate_old_data(), self.target)
