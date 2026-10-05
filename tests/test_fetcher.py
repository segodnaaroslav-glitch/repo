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
