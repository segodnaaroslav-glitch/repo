import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from supreme import cdp, dreampets, fetcher, markets

ROOT = str(Path(__file__).resolve().parent.parent)


def _test_browser():
    path = os.environ.get("MM2_TEST_BROWSER") or fetcher.find_system_browser()
    if not path and os.path.exists("/opt/pw-browsers/chromium"):
        path = "/opt/pw-browsers/chromium"
    return path


def _uuid(number):
    return f"00000000-0000-4000-8000-{number:012d}"


def two_link_cards(numbers):
    """Карточки как на настоящих площадках: ссылка-картинка и ссылка-название ведут на один
    товар, цена и лоты — вне обеих ссылок."""
    return "".join(
        f'<div class="card"><a href="/mm2/product/item-{n}/{_uuid(n)}"><img src="/pic.png" alt=""></a>'
        f'<a href="/mm2/product/item-{n}/{_uuid(n)}"><p>Item {n}</p></a>'
        f'<b>от {100 + n},50 ₽</b><i>{n} лотов</i></div>' for n in numbers)


def grid(numbers, extra=""):
    return f'<html><head><title>Рынок</title></head><body><div class="grid">{two_link_cards(numbers)}</div>{extra}</body></html>'


class Site:
    """Локальный сайт: путь -> HTML, (код, тип, тело), функция(handler) или "hang"; журнал запросов."""

    def __init__(self, routes):
        self.routes = routes
        self.log = []
        self.release = threading.Event()
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                site.log.append((time.time(), self.path, {k.lower(): v for k, v in self.headers.items()}))
                route = site.routes.get(self.path)
                if route == "hang":
                    site.release.wait(30)
                    return
                if callable(route):
                    route = route(self)
                if route is None:
                    route = (404, "text/html", "")
                if isinstance(route, str):
                    route = (200, "text/html; charset=utf-8", route)
                status, kind, body = route
                data = body.encode("utf-8") if isinstance(body, str) else body
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", kind)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def requested(self, path):
        return [entry for entry in self.log if entry[1] == path]

    def close(self):
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()


def _alive_with(text):
    """Живые процессы (не зомби), в командной строке которых есть text (Linux; без /proc — пусто)."""
    found = []
    for pid in os.listdir("/proc") if os.path.isdir("/proc") else []:
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as file:
                command = file.read()
            with open(f"/proc/{pid}/stat", encoding="latin-1") as file:
                state = file.read().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            continue
        if text.encode() in command and state not in ("Z", "X"):
            found.append(int(pid))
    return found


def _wait_gone(text, seconds=10):
    deadline = time.time() + seconds
    while time.time() < deadline and _alive_with(text):
        time.sleep(0.2)
    return _alive_with(text)


# --- без браузера -------------------------------------------------------------------

def _frame(payload, opcode=0x1, fin=True):
    head = bytes([(0x80 if fin else 0) | opcode])
    if len(payload) < 126:
        head += bytes([len(payload)])
    elif len(payload) < 65536:
        head += bytes([126]) + struct.pack(">H", len(payload))
    else:
        head += bytes([127]) + struct.pack(">Q", len(payload))
    return head + payload


class FakeDevTools:
    """Сервер WebSocket: отвечает на рукопожатие, дальше шлёт то, что скажет тест."""

    def __init__(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.conn = None
        self.ready = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        conn, _ = self.listener.accept()
        data = b""
        while b"\r\n\r\n" not in data:
            data += conn.recv(4096)
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
        self.conn = conn
        self.ready.set()

    def client(self):
        ws = cdp.WebSocket(f"ws://127.0.0.1:{self.listener.getsockname()[1]}/devtools/browser/x", timeout=5)
        self.ready.wait(5)
        return ws

    def close(self):
        for item in (self.conn, self.listener):
            try:
                item.close()
            except (OSError, AttributeError):
                pass


class WebSocketTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeDevTools()
        self.addCleanup(self.server.close)
        self.ws = self.server.client()
        self.addCleanup(self.ws.close)

    def test_big_message_is_read_in_linear_time(self):
        payload = b'{"id":1,"result":"' + b"x" * 32_000_000 + b'"}'
        threading.Thread(target=self.server.conn.sendall, args=(_frame(payload),), daemon=True).start()
        started = time.time()
        text = self.ws.recv(30)
        self.assertEqual(len(text), len(payload))
        self.assertLess(time.time() - started, 5)  # раньше буфер копировался целиком на каждый кусок

    def test_deadline_holds_while_data_keeps_trickling(self):
        stop = threading.Event()
        self.addCleanup(stop.set)

        def trickle():
            self.server.conn.sendall(_frame(b"x" * 10_000_000)[:10])
            while not stop.is_set():
                try:
                    self.server.conn.sendall(b"x" * 1000)
                except OSError:
                    return
                time.sleep(0.01)

        threading.Thread(target=trickle, daemon=True).start()
        started = time.time()
        with self.assertRaises(TimeoutError):
            self.ws.recv(0.5)
        self.assertLess(time.time() - started, 3)

    def test_half_frame_survives_a_timeout(self):
        data = _frame(b'{"id":7}')
        self.server.conn.sendall(data[:5])
        with self.assertRaises(TimeoutError):
            self.ws.recv(0.3)
        self.server.conn.sendall(data[5:])
        self.assertEqual(self.ws.recv(2), '{"id":7}')

    def test_ping_and_fragments(self):
        self.server.conn.sendall(_frame(b"pi", opcode=0x9) + _frame(b'{"a":', fin=False) + _frame(b"1}", opcode=0x0))
        self.assertEqual(self.ws.recv(2), '{"a":1}')
        self.server.conn.settimeout(2)
        pong = self.server.conn.recv(8)
        self.assertEqual(pong[0], 0x8A)
        key = pong[2:6]
        self.assertEqual(bytes(b ^ key[i % 4] for i, b in enumerate(pong[6:8])), b"pi")

    def test_closed_connection(self):
        self.server.conn.close()
        with self.assertRaises(cdp.ClosedError):
            self.ws.recv(2)


@unittest.skipIf(os.name == "nt", "каналы — только в Linux/macOS")
class PipeTests(unittest.TestCase):
    def test_messages_split_by_zero_byte(self):
        their_read, our_write = os.pipe()
        our_read, their_write = os.pipe()
        pipe = cdp.Pipe(our_read, our_write)
        self.addCleanup(pipe.close)
        self.addCleanup(os.close, their_read)
        pipe.send('{"id":1}')
        self.assertEqual(os.read(their_read, 100), b'{"id":1}\0')
        os.write(their_write, b'{"a":1}\0{"b"')
        self.assertEqual(pipe.recv(1), '{"a":1}')
        with self.assertRaises(TimeoutError):
            pipe.recv(0.2)
        os.write(their_write, b":2}\0")
        self.assertEqual(pipe.recv(1), '{"b":2}')
        os.close(their_write)
        with self.assertRaises(cdp.ClosedError):
            pipe.recv(1)


class HousekeepingTests(unittest.TestCase):
    def test_stale_profiles_are_swept(self):
        with tempfile.TemporaryDirectory() as folder:
            old, new, other = (Path(folder) / name for name in ("mm2values-cdp-old", "mm2values-cdp-new", "other-old"))
            for path in (old, new, other):
                (path / "Default").mkdir(parents=True)
            hours_ago = time.time() - 2 * 3600
            for path in (old, other):
                os.utime(path, (hours_ago, hours_ago))
            self.assertEqual(cdp.sweep_stale_profiles(folder=folder), 1)
            self.assertEqual(sorted(p.name for p in Path(folder).iterdir()), ["mm2values-cdp-new", "other-old"])

    def test_windows_job_structure_size(self):
        import ctypes
        size = ctypes.sizeof(cdp._job_limit_structure())
        self.assertEqual(size, 144 if struct.calcsize("P") == 8 else 112)  # как sizeof в Windows SDK

    @unittest.skipIf(os.name == "nt", "проверка для Linux/macOS")
    def test_windows_job_is_not_touched_elsewhere(self):
        self.assertFalse(cdp._windows_job_assign(unittest.mock.Mock()))
        self.assertIsNone(cdp._STATE["job"])

    def test_devtools_discovery_bypasses_proxy(self):
        site = Site({"/json/version": (200, "application/json", '{"webSocketDebuggerUrl": "ws://x"}')})
        self.addCleanup(site.close)
        dead = "http://127.0.0.1:9"
        env = {"http_proxy": dead, "HTTP_PROXY": dead, "all_proxy": dead, "no_proxy": "", "NO_PROXY": ""}
        with unittest.mock.patch.dict(os.environ, env):
            self.assertEqual(cdp.local_json(site.base + "/json/version"), {"webSocketDebuggerUrl": "ws://x"})

    def test_no_new_browser_while_exiting(self):
        with unittest.mock.patch.dict(cdp._STATE, {"exiting": True}), \
                unittest.mock.patch.object(cdp.subprocess, "Popen") as popen:
            with self.assertRaises(cdp.BrowserError):
                cdp.Browser(executable="/bin/true")
        popen.assert_not_called()

    def test_monitor_stop_closes_browsers(self):
        with tempfile.TemporaryDirectory() as folder:
            monitor = markets.MarketMonitor(Path(folder) / "values.json", [])
            with unittest.mock.patch.object(markets.cdp, "close_all") as close_all:
                monitor.stop()
            close_all.assert_called_once_with()
            self.assertTrue(monitor._stop.is_set())

    def test_program_exit_stops_monitor(self):
        import argparse
        import mm2_values
        httpd = unittest.mock.Mock(server_address=("127.0.0.1", 1), sync=None)
        httpd.serve_forever.side_effect = KeyboardInterrupt
        monitor = unittest.mock.Mock()
        args = argparse.Namespace(port=1, no_browser=True, no_update=True, no_auto=True, no_markets=False)
        with tempfile.TemporaryDirectory() as folder, \
                unittest.mock.patch.object(mm2_values, "running_instance", return_value=None), \
                unittest.mock.patch.object(mm2_values.paths, "migrate_old_data", return_value=Path(folder)), \
                unittest.mock.patch.object(mm2_values.store, "use_data_dir"), \
                unittest.mock.patch.object(mm2_values.store, "DATA_FILE", Path(folder) / "values.json"), \
                unittest.mock.patch.object(mm2_values.markets, "MarketMonitor", return_value=monitor), \
                unittest.mock.patch.object(mm2_values.rates, "RateWatcher"), \
                unittest.mock.patch.object(mm2_values.server, "make_server", return_value=httpd), \
                unittest.mock.patch("builtins.print"):
            mm2_values.cmd_run(args)
        monitor.start.assert_called_once_with()
        monitor.stop.assert_called_once_with()
        httpd.server_close.assert_called_once_with()


# --- настоящий браузер ---------------------------------------------------------------

SELECTOR = 'a[href*="/mm2/product/"]'

BUTTON_MORE = """<html><body><div id="list"></div><button id="more">Показать ещё</button><script>
const list = document.getElementById('list'), more = document.getElementById('more');
let page = 0;
const add = () => {
  for (let i = 1; i <= 3; i++) {
    const n = page * 3 + i, id = '00000000-0000-4000-8000-' + String(n).padStart(12, '0');
    list.insertAdjacentHTML('beforeend', '<div><a href="/mm2/product/item-' + n + '/' + id + '"><p>Item ' + n +
      '</p></a><b>от ' + n + ' ₽</b></div>');
  }
  if (++page >= 3) more.remove();
};
add();
more.onclick = add;
</script></body></html>"""


def _card(n, inner):
    return f'<a href="/mm2/product/item-{n}/{_uuid(n)}">{inner}</a>'


# Карточки, где у надписи несколько текстовых узлов: цена — целая и дробная части в разных
# span, название — из нескольких узлов (как рисуют React и Vue), скрытая старая цена.
SPLIT_TEXT = ("<html><body><div class='grid'>"
              + _card(1, "<p>Item 1</p><b>от <span>231</span><span>,71</span> ₽</b><i>1 лотов</i>")
              + _card(2, "<p><b>Chroma</b> Seer</p><b>от 101 ₽</b><i>2 лотов</i>")
              + _card(3, "<p id='react'></p><b>от <span>1 234</span>.<span>50</span> ₽</b>")
              + _card(4, "<p>Item 4</p><s style='display:none'>от 999 ₽</s><b>от 44 ₽</b><i>4</i> лотов")
              + _card(5, "<p>Item 5</p><i>114</i><b>от 230 ₽</b>")
              + "</div><script>document.getElementById('react').append('Chroma', ' ', 'Lugers')</script></body></html>")


def page_list(numbers, more=""):
    return f"<html><body><div class='grid'>{two_link_cards(numbers)}</div>{more}</body></html>"


def form_more(page):
    return f'<form action=""><input type="hidden" name="page" value="{page}"><button>Показать ещё</button></form>'


@unittest.skipUnless(_test_browser(), "нет Edge/Chrome для проверки скрытого браузера")
class BrowserPageTests(unittest.TestCase):
    """Один браузер на все проверки: открывается каждая страница по очереди."""

    @classmethod
    def setUpClass(cls):
        cls.site = Site({
            "/grid": grid(range(1, 11)),
            "/pic.png": (200, "image/png", b""),
            "/buttons": BUTTON_MORE,
            "/paged": grid(range(1, 4), '<a href="?page=2">Показать ещё</a>'),
            "/paged?page=2": grid(range(4, 7)),
            "/challenge": "<html><body>Just a moment...<script>setTimeout(() => location.replace('/grid'), 2500)"
                          "</script></body></html>",
            "/stuck": grid(range(1, 4), '<img src="/never">'),
            "/never": "hang",
            "/hop": "<html><body><script>setTimeout(() => location.replace('/plain'), 500)</script></body></html>",
            "/plain": "<html><body>ok</body></html>",
            "/split": SPLIT_TEXT,
            "/early": "<html><head><script>location.replace('/grid')</script></head><body></body></html>",
            # «Показать ещё» уводит на другой адрес: отправка формы, переход скриптом, ссылка на ту же страницу.
            "/f": page_list(range(1, 4), form_more(2)),
            "/f?page=2": page_list(range(4, 7), "<button onclick=\"location.href='/f?page=3'\">Показать ещё</button>"),
            "/f?page=3": page_list(range(7, 10), '<a href="/f?page=4"><button>Показать ещё</button></a>'),
            "/f?page=4": page_list(range(10, 13)),
            "/loop": page_list(range(1, 4), form_more(2)),
            "/loop?page=2": page_list(range(4, 7), form_more(2)),
            "/self": page_list(range(1, 4), '<a href="/self">Показать ещё</a>'),
            "/pg": page_list(range(1, 4), '<a href="?page=2">Показать ещё</a>'),
            **{f"/pg?page={n}": page_list(range(3 * n - 2, 3 * n + 1), f'<a href="?page={n + 1}">Показать ещё</a>'
                                         if n < 5 else "") for n in range(2, 6)},
            "/a": '{"page": "a"}',
            "/b": '{"page": "b"}',
            "/c": '{"page": "c"}',
            "/limit": (429, "text/html", "Too Many Requests"),
            "/socket.io/?EIO=4&transport=polling": "hang",
            "/product": ('<html><head><title>Harvester — купить в ММ2</title>'
                         '<meta name="description" content="Купить Harvester от 328.98 ₽, 674 лотов.">'
                         '<style>' + "x" * 2_000_000 + '</style>'
                         '<script type="application/ld+json">{"@type": "Product", "name": "Harvester"}</script>'
                         '<script>var bundle = "' + "y" * 1_000_000 + '";</script></head>'
                         '<body><svg>' + "<path/>" * 100_000 + '</svg><p>от 328.98 ₽</p>' + "z" * 2_000_000
                         + '</body></html>'),
        })
        cls.browser = cdp.Browser(_test_browser())

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.site.close()

    def setUp(self):
        self.browser.notes.clear()

    def collect(self, path, **kwargs):
        self.browser.open(self.site.base + path, settle=0, load_wait=kwargs.pop("load_wait", 10))
        return self.browser.collect_cards(SELECTOR, pause=0.2, stable_rounds=2, **kwargs)

    def test_cards_with_image_link_title_link_and_price_outside(self):
        result = self.collect("/grid")
        self.assertEqual(len(result["cards"]), 10)
        for card in result["cards"]:
            n = int(card["href"].rsplit("-", 1)[1])
            self.assertEqual(dreampets.parse_card_text(card["text"]),
                             {"name": f"Item {n}", "price": 100 + n + 0.5, "lots": n, "rarity": None})
        self.assertEqual(result["title"], "Рынок")

    def test_card_text_keeps_split_prices_and_names(self):
        result = self.collect("/split")
        found = {int(c["href"].rsplit("-", 1)[1]): {k: v for k, v in dreampets.parse_card_text(c["text"]).items()
                                                     if k != "rarity"} for c in result["cards"]}
        self.assertEqual(found, {
            1: {"name": "Item 1", "price": 231.71, "lots": 1},  # не 71: "231" и ",71" — одна надпись
            2: {"name": "Chroma Seer", "price": 101, "lots": 2},  # не "Seer"
            3: {"name": "Chroma Lugers", "price": 1234.5, "lots": None},
            4: {"name": "Item 4", "price": 44, "lots": 4},  # скрытая цена не читается
            5: {"name": "Item 5", "price": 230, "lots": None},  # не 114230
        })

    def test_load_more_button_is_pressed(self):
        result = self.collect("/buttons")
        self.assertEqual(sorted(dreampets.parse_card_text(c["text"])["price"] for c in result["cards"]),
                         list(range(1, 10)))
        self.assertEqual(result["pages"], 1)

    def test_load_more_link_opens_next_page_and_keeps_the_first(self):
        result = self.collect("/paged")
        self.assertEqual(sorted(int(c["href"].rsplit("-", 1)[1]) for c in result["cards"]), list(range(1, 7)))
        self.assertEqual(result["pages"], 2)
        self.assertEqual(len(self.site.requested("/paged?page=2")), 1)

    def test_load_more_that_navigates_keeps_every_page(self):
        result = self.collect("/f")
        self.assertEqual(sorted(int(c["href"].rsplit("-", 1)[1]) for c in result["cards"]), list(range(1, 13)))
        self.assertEqual(result["pages"], 4)
        self.assertEqual([len(self.site.requested(p)) for p in ("/f", "/f?page=2", "/f?page=3", "/f?page=4")],
                         [1, 1, 1, 1])

    def test_load_more_that_keeps_navigating_is_pressed_once_per_address(self):
        result = self.collect("/loop")
        self.assertEqual(sorted(int(c["href"].rsplit("-", 1)[1]) for c in result["cards"]), list(range(1, 7)))
        self.assertLessEqual(len(self.site.requested("/loop?page=2")), 2)
        before = len(self.site.requested("/self"))
        result = self.collect("/self")  # ссылка на эту же страницу, а она перезагружается
        self.assertEqual(len(result["cards"]), 3)
        self.assertLessEqual(len(self.site.requested("/self")) - before, 2)

    def test_link_pages_continue_where_the_last_pass_stopped(self):
        def numbers(result):
            return sorted(int(c["href"].rsplit("-", 1)[1]) for c in result["cards"])

        first = self.collect("/pg", max_pages=2)
        self.assertEqual(numbers(first), list(range(1, 7)))
        self.assertTrue(first["next"].endswith("/pg?page=3"))
        second = self.collect("/pg", max_pages=3, resume=first["next"])
        self.assertEqual(numbers(second), [1, 2, 3] + list(range(7, 13)))  # первая страница и дальше 3-я, 4-я
        self.assertTrue(second["next"].endswith("/pg?page=5"))
        last = self.collect("/pg", max_pages=3, resume=second["next"])
        self.assertEqual(numbers(last), [1, 2, 3, 13, 14, 15])
        self.assertIsNone(last["next"])  # дошли до конца — в следующий раз с начала
        self.assertEqual(numbers(self.collect("/pg")), list(range(1, 16)))

    def test_redirect_before_dom_ready_does_not_wait_for_the_timeout(self):
        started = time.time()
        result = self.collect("/early")
        self.assertLess(time.time() - started, 15)
        self.assertEqual(len(result["cards"]), 10)
        self.assertFalse([note for note in self.browser.notes if "не загрузилась" in note])

    def test_page_that_navigates_after_load(self):
        result = self.collect("/challenge")
        self.assertEqual(len(result["cards"]), 10)
        self.assertTrue(result["url"].endswith("/grid"))

    def test_missing_load_event_is_not_fatal(self):
        started = time.time()
        result = self.collect("/stuck", load_wait=2)
        self.assertLess(time.time() - started, 15)
        self.assertEqual(len(result["cards"]), 3)
        self.assertTrue(any("не догрузилась" in note for note in self.browser.notes))

    def test_evaluate_retries_on_the_new_page(self):
        self.browser.open(self.site.base + "/hop", settle=0)
        script = "(async () => { await new Promise((r) => setTimeout(r, 1500)); return location.pathname; })()"
        self.assertEqual(self.browser.evaluate(script, timeout=20, retries=1), "/plain")

    def test_fetch_many_one_by_one_and_stops_at_block(self):
        self.browser.open(self.site.base + "/plain", settle=0)
        before = len(self.site.log)
        urls = [self.site.base + p for p in ("/a", "/b", "/limit", "/c")]
        out, stop = self.browser.fetch_many(urls, pause=0.3)
        self.assertEqual([out[u][0] for u in urls[:3]], [200, 200, 429])
        self.assertEqual(stop, "HTTP 429")
        self.assertNotIn(urls[3], out)
        mine = self.site.log[before:]
        self.assertEqual([entry[1] for entry in mine], ["/a", "/b", "/limit"])  # после отказа — ни одного запроса
        times = [entry[0] for entry in mine]
        self.assertGreaterEqual(min(b - a for a, b in zip(times, times[1:])), 0.25)

    def test_fetch_many_aborts_a_hanging_request_and_skips_streams(self):
        self.browser.open(self.site.base + "/plain", settle=0)
        urls = [self.site.base + p for p in ("/a", "/never", "/socket.io/?EIO=4&transport=polling", "/c")]
        started = time.time()
        out, stop = self.browser.fetch_many(urls, pause=0.1, timeout=2)
        self.assertLess(time.time() - started, 10)
        self.assertIsNone(stop)
        self.assertEqual((out[urls[0]][0], out[urls[1]][0], out[urls[3]][0]), (200, 0, 200))
        self.assertNotIn(urls[2], out)
        self.assertEqual(self.site.requested("/socket.io/?EIO=4&transport=polling"), [])

    def test_fetched_page_is_cleaned_and_capped(self):
        self.browser.open(self.site.base + "/plain", settle=0)
        out, _ = self.browser.fetch_many([self.site.base + "/product"])
        status, text = out[self.site.base + "/product"]
        self.assertEqual(status, 200)
        self.assertLessEqual(len(text), cdp.MAX_PAGE_CHARS)
        self.assertIn("application/ld+json", text)
        self.assertNotIn("yyyy", text)
        self.assertNotIn("xxxx", text)
        parsed = dreampets.parse_product_page(text)
        self.assertEqual((parsed["price"], parsed["lots"]), (328.98, 674))

    def test_looks_like_a_regular_browser(self):
        self.browser.open(self.site.base + "/plain", settle=0)
        headers = self.site.requested("/plain")[-1][2]
        self.assertNotIn("Headless", headers["user-agent"])
        self.assertIn("sec-ch-ua", headers)  # подсказки клиента есть и совпадают с версией
        self.assertNotIn("Headless", headers["sec-ch-ua"])
        self.assertEqual(headers["accept-language"], "ru-RU,ru;q=0.9,en;q=0.8")
        webdriver, agent, brands = self.browser.evaluate(
            "[navigator.webdriver, navigator.userAgent, navigator.userAgentData.brands.map((b) => b.brand)]")
        self.assertFalse(webdriver)
        self.assertNotIn("Headless", agent)
        self.assertTrue(brands)
        major = agent.split("Chrome/")[1].split(".")[0]
        self.assertIn(f'v="{major}"', headers["sec-ch-ua"])


@unittest.skipUnless(_test_browser(), "нет Edge/Chrome для проверки скрытого браузера")
class BrowserLifetimeTests(unittest.TestCase):
    def test_port_mode_ignores_system_proxy(self):
        site = Site({"/plain": "<html><body>ok</body></html>"})
        self.addCleanup(site.close)
        dead = "http://127.0.0.1:9"
        env = {"http_proxy": dead, "HTTP_PROXY": dead, "all_proxy": dead, "no_proxy": "", "NO_PROXY": ""}
        with unittest.mock.patch.dict(os.environ, env):
            with cdp.Browser(_test_browser(), pipe=False, timeout=20) as browser:
                browser.open(site.base + "/plain", settle=0)
                self.assertEqual(browser.evaluate("document.body.textContent"), "ok")

    def test_close_all_kills_live_browsers(self):
        browser = cdp.Browser(_test_browser())
        self.addCleanup(browser.close)
        profile, process = browser.profile, browser.process
        self.assertIn(browser, cdp.live_browsers())
        cdp.close_all()
        self.assertNotIn(browser, cdp.live_browsers())
        self.assertIsNotNone(process.poll())
        if os.path.isdir("/proc"):  # дочерние процессы браузера и профиль — проверка через /proc (не Windows)
            self.assertEqual(_wait_gone(profile), [])
            self.assertFalse(os.path.exists(profile))
        browser.close()  # владелец закрывает ещё раз — без ошибок

    @unittest.skipIf(sys.platform.startswith("win"), "проверка через /proc")
    def test_browser_of_a_daemon_thread_is_closed_at_exit(self):
        for pipe, ending in ((False, "sys.exit(0)"), (True, "os._exit(0)")):
            with self.subTest(pipe=pipe):
                script = (
                    "import os, sys, threading, time\n"
                    f"sys.path.insert(0, {ROOT!r})\n"
                    "from supreme import cdp\n"
                    "ready = threading.Event()\n"
                    "def run():\n"
                    f"    browser = cdp.Browser({_test_browser()!r}, pipe={pipe!r})\n"
                    "    print(browser.profile, flush=True)\n"
                    "    ready.set()\n"
                    "    time.sleep(60)\n"
                    "threading.Thread(target=run, daemon=True).start()\n"
                    "ready.wait(60)\n"
                    f"{ending}\n")
                done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=90)
                profile = done.stdout.strip()
                self.assertTrue(profile.startswith(os.path.join(tempfile.gettempdir(), cdp.PROFILE_PREFIX)), done.stderr)
                # TCP: закрыл atexit (и убрал профиль); каналы: браузер закрылся сам, когда программа исчезла.
                self.assertEqual(_wait_gone(profile), [])
                if not pipe:
                    self.assertFalse(os.path.exists(profile))
                else:
                    import shutil
                    shutil.rmtree(profile, ignore_errors=True)


# --- проход браузера DreamPets ------------------------------------------------------

def _adapter(base, cache):
    adapter = markets.DreamPetsAdapter({"id": "dreampets", "title": "DreamPets", "kind": "dreampets",
                                        "url": base + "/mm2/"}, cache)
    adapter.root = base
    adapter.report = {"steps": []}
    adapter.sources, adapter.fresh, adapter.fresh_price = {}, set(), set()
    return adapter


class FakeBrowser:
    """Вместо браузера: страницы товаров отвечают по номеру товара (журнал — requested)."""

    requested = []
    answers = {}  # номер -> (код, текст)
    result = {"cards": [], "api": [], "title": "", "pages": 1, "url": None, "next": None}

    def __init__(self, *args, **kwargs):
        self.notes = []
        self.kwargs = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def open(self, url, **kwargs):
        pass

    def collect_cards(self, selector, **kwargs):
        FakeBrowser.collect_kwargs = kwargs
        return dict(self.result)

    def fetch_many(self, urls, **kwargs):
        out = {}
        for url in urls:
            FakeBrowser.requested.append(url)
            out[url] = self.answers[int(url.rsplit("-", 1)[1])]
        return out, None


class DreamPetsBrowserPassTests(unittest.TestCase):
    def setUp(self):
        FakeBrowser.requested = []
        patch = unittest.mock.patch.object(markets.cdp, "Browser", FakeBrowser)
        patch.start()
        self.addCleanup(patch.stop)

    def test_failing_product_pages_do_not_pin_the_queue(self):
        base = "https://dreampets.gg"
        page = ('<html><head><title>Item {n} — купить в ММ2</title><meta name="description" '
                'content="Купить Item {n} от {n}0 ₽, {n} лотов."></head></html>')
        products = {}
        for n in range(1, 41):  # 20 отвечают 500, 10 не отвечают (прерваны), 10 хорошие — в конце
            products[_uuid(n)] = {"name": f"Item {n}", "url": f"{base}/mm2/product/item-{n}/{_uuid(n)}"}
        FakeBrowser.answers = {n: (500, "") if n <= 20 else (0, "запрос прерван") if n <= 30
                               else (200, page.format(n=n)) for n in range(1, 41)}
        adapter = _adapter(base, {})
        for _ in range(2):
            adapter._browser_pass(base + "/mm2/", products, time.time(), read_pages=True)
            time.sleep(0.01)
        good = [u for u in FakeBrowser.requested if int(u.rsplit("-", 1)[1]) > 30]
        self.assertEqual(len(good), 10)  # во втором проходе — уже хорошие страницы
        self.assertEqual(products[_uuid(35)]["price"], 350)
        self.assertTrue(all(products[_uuid(n)].get("page_at") for n in range(1, 41)))

    def test_refusal_that_stops_the_pass_is_not_counted(self):
        base = "https://dreampets.gg"
        products = {_uuid(n): {"name": f"Item {n}", "url": f"{base}/mm2/product/item-{n}/{_uuid(n)}"} for n in (1, 2)}
        FakeBrowser.answers = {1: (404, ""), 2: (429, "Too Many Requests")}
        _adapter(base, {})._browser_pass(base + "/mm2/", products, time.time(), read_pages=True)
        self.assertTrue(products[_uuid(1)].get("page_at"))
        self.assertFalse(products[_uuid(2)].get("page_at"))  # отказ площадки — в следующий раз снова первой

    def test_next_market_page_is_remembered_between_passes(self):
        cache = {}
        with unittest.mock.patch.dict(FakeBrowser.result, {"next": "https://dreampets.gg/mm2/?page=11"}):
            _adapter("https://dreampets.gg", cache)._browser_pass("https://dreampets.gg/mm2/", {}, time.time(), False)
        self.assertEqual(cache["browser_next"], "https://dreampets.gg/mm2/?page=11")
        _adapter("https://dreampets.gg", cache)._browser_pass("https://dreampets.gg/mm2/", {}, time.time(), False)
        self.assertEqual(FakeBrowser.collect_kwargs.get("resume"), "https://dreampets.gg/mm2/?page=11")
        self.assertIsNone(cache["browser_next"])  # дошли до конца — в следующий раз с начала


SPA_WITH_GUEST_API = """<html><head><title>DreamPets — MM2</title></head><body><div id="app"></div><script>
fetch('/api/me').catch(() => null).then(() => fetch('/api/catalog?game=mm2')).then((r) => r.json()).then((data) => {
  for (const p of data.items)
    document.getElementById('app').insertAdjacentHTML('beforeend', '<div><a href="/mm2/product/' + p.slug + '/'
      + p.id + '"><p>' + p.title + '</p></a><b>от ' + p.minPrice + ' ₽</b></div>');
});
</script></body></html>"""


@unittest.skipUnless(_test_browser(), "нет Edge/Chrome для проверки скрытого браузера")
class DreamPetsBrowserApiTests(unittest.TestCase):
    def test_forbidden_guest_endpoint_does_not_stop_api_discovery(self):
        items = [{"id": _uuid(n), "slug": f"item-{n}", "title": f"Item {n}", "minPrice": 10 + n, "lotsCount": n}
                 for n in range(1, 13)]
        site = Site({"/mm2/": SPA_WITH_GUEST_API, "/api/me": (403, "application/json", '{"error": "guest"}'),
                     "/api/catalog?game=mm2": (200, "application/json", json.dumps({"items": items}))})
        self.addCleanup(site.close)
        cache = {}
        with unittest.mock.patch.object(markets.fetcher, "find_system_browser", return_value=_test_browser()):
            _adapter(site.base, cache)._browser_pass(site.base + "/mm2/", cache.setdefault("products", {}),
                                                     time.time(), read_pages=False)
        self.assertEqual(cache.get("api_urls"), [site.base + "/api/catalog?game=mm2"])
        self.assertEqual(len(cache["products"]), 12)


if __name__ == "__main__":
    unittest.main()
