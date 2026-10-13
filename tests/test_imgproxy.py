"""Сравнение плиток по картинке, серверная часть: картинки предметов через свой сервер
(supreme/imgproxy.py), признаки картинок (data/picfeat.json), плитки и «picked» в /api/calc,
повторное чтение значков «xN» (/api/calc/badges)."""

import base64
import http.client
import json
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from supreme import imgproxy, markets, ocr, parser, paths, server, store, tileocr
from tests import picgen

IEND = b"\x00\x00\x00\x00IEND\xaeB`\x82"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40 + IEND
WEBP = b"RIFF\x20\x00\x00\x00WEBPVP8 " + b"\x00" * 24  # размер RIFF = длина файла − 8
GIF = b"GIF89a" + b"\x00" * 20 + b";"
BIG_PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 5000 + IEND


class Upstream(BaseHTTPRequestHandler):
    """Сайт картинок (вместо cdn.starpets.gg): обычные картинки и всё, что картинкой не является."""

    calls = {}
    routes = {
        "/ok.webp": (200, "image/webp", WEBP),
        "/raw.png": (200, "application/octet-stream", PNG),  # тип неизвестен, но байты — PNG
        "/html.png": (200, "image/png", b"<html><script>alert(1)</script></html>"),
        "/text.webp": (200, "text/plain", WEBP),
        "/MM/110x110/Luger%20chroma.webp": (200, "image/webp", WEBP),  # у StarPets бывают пробелы в адресе
        "/svg.svg": (200, "image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>"),
        "/big.webp": (200, "image/webp", WEBP + b"\x00" * (imgproxy.MAX_BYTES + 10)),
        "/busy.webp": (503, "text/plain", b"busy"),
    }

    def log_message(self, *args):
        pass

    def do_GET(self):
        Upstream.calls[self.path] = Upstream.calls.get(self.path, 0) + 1
        time.sleep(0.02)
        if self.path == "/redirect.webp":
            self.send_response(302)
            self.send_header("Location", "https://evil.example/x.webp")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path in ("/short.png", "/cut-unsized.png", "/cut-chunked.png"):  # связь оборвалась посреди картинки
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            if self.path == "/short.png":
                self.send_header("Content-Length", str(len(BIG_PNG)))
            if self.path == "/cut-chunked.png":
                self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            part = BIG_PNG[:1000]
            self.wfile.write(b"%x\r\n" % len(BIG_PNG) + part if self.path == "/cut-chunked.png" else part)
            self.wfile.flush()
            self.close_connection = True
            return
        if self.path in ("/forbidden.webp", "/legal.webp", "/challenge.webp"):  # сайт не пускает программу
            status, ctype = {"/forbidden.webp": (403, "text/plain"), "/legal.webp": (451, "text/plain"),
                             "/challenge.webp": (200, "text/html; charset=utf-8")}[self.path]
            body = b"<html>Request unsuccessful. Incapsula incident ID: 1</html>"
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/unsized.webp":  # без Content-Length: размер проверяется при чтении
            self.send_response(200)
            self.send_header("Content-Type", "image/webp")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(WEBP + b"\x00" * (imgproxy.MAX_BYTES + 10))
            except OSError:
                pass
            return
        status, ctype, body = self.routes.get(self.path, (404, "text/plain", b"no"))
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass  # слишком большую картинку программа не дочитывает


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ImageCacheTests(unittest.TestCase):
    def setUp(self):
        Upstream.calls = {}
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        base = f"http://127.0.0.1:{self.upstream.server_address[1]}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), imgproxy._CheckedRedirects)
        # Настоящая загрузка (_download), только https://cdn.starpets.gg отвечает местный сервер.
        self.download = lambda url: imgproxy._download(url.replace("https://cdn.starpets.gg", base), opener=opener)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name) / "images"
        self.clock = Clock()
        self.cache = imgproxy.ImageCache(self.folder, download=self.download, clock=self.clock)

    def url(self, name):
        return "https://cdn.starpets.gg/" + name

    def status(self, url):
        try:
            self.cache.get(url)
        except imgproxy.ImageError as error:
            return error.status
        return 200

    def test_only_https_on_the_two_sites(self):
        for url in ("http://cdn.starpets.gg/ok.webp", "https://evil.example/x.webp", "https://cdn.starpets.gg@evil.example/x",
                    "https://cdn.starpets.gg.evil.example/x.webp", "https://evil.example/cdn.starpets.gg/x.webp",
                    "https://cdn.starpets.gg:8443/x.webp", "https://user:pw@cdn.starpets.gg/x.webp",
                    "https://notstarpets.gg/x.webp", "file:///etc/passwd", "//cdn.starpets.gg/x.webp", "", None):
            self.assertFalse(imgproxy.host_allowed(url), url)
            self.assertEqual(self.status(url), 403, url)
        for url in ("https://supremevalues.com/mm2/items/a.webp", "https://www.supremevalues.com/a.png",
                    "https://cdn.starpets.gg/MM/110x110/Gun1.webp", "https://CDN.StarPets.gg:443/x.webp"):
            self.assertTrue(imgproxy.host_allowed(url), url)
        self.assertEqual(Upstream.calls, {})  # чужие адреса даже не запрашивались

    def test_only_raster_pictures_of_sane_size(self):
        self.assertEqual(self.cache.get(self.url("ok.webp")), (WEBP, "image/webp"))
        self.assertEqual(self.cache.get(self.url("raw.png")), (PNG, "image/png"))  # тип — по самим байтам
        for name, status in (("html.png", 502), ("text.webp", 502), ("svg.svg", 502), ("big.webp", 502),
                             ("unsized.webp", 502), ("redirect.webp", 502), ("missing.webp", 404), ("busy.webp", 503)):
            self.assertEqual(self.status(self.url(name)), status, name)
        self.assertEqual(sorted(p.suffix for p in self.folder.iterdir() if p.name != "failed.json"), [".png", ".webp"])
        self.assertEqual(imgproxy.sniff(GIF), ("image/gif", ".gif"))
        self.assertIsNone(imgproxy.sniff(b"<svg"))

    def test_addresses_with_spaces(self):
        # Настоящие адреса StarPets: «.../110x110/Luger chroma.webp» — пробел кодируется, а не обрывает запрос.
        self.assertEqual(self.cache.get(self.url("MM/110x110/Luger chroma.webp")), (WEBP, "image/webp"))
        self.assertEqual(Upstream.calls, {"/MM/110x110/Luger%20chroma.webp": 1})
        self.assertEqual(imgproxy.quote_url("https://cdn.starpets.gg/a b/%D0%9A ё.webp?x=1 2&y=/z"),
                         "https://cdn.starpets.gg/a%20b/%D0%9A%20%D1%91.webp?x=1%202&y=/z")

    def test_every_failure_gets_an_answer(self):
        # Ошибки http.client — тоже ImageError (ответ браузеру), а не обрыв запроса без ответа.
        for error, status, kind in ((http.client.InvalidURL("bad"), 502, "final"),
                                    (http.client.IncompleteRead(b"x", 10), 503, "network"),
                                    (http.client.LineTooLong("header"), 503, "network"),
                                    (http.client.BadStatusLine("?"), 503, "network")):
            opener = mock.Mock()
            opener.open.side_effect = error
            with self.assertRaises(imgproxy.ImageError) as caught:
                imgproxy._download(self.url("x.webp"), opener=opener)
            self.assertEqual((caught.exception.status, caught.exception.kind), (status, kind), error)

    def test_cut_off_pictures_are_not_cached(self):
        for name, kind in (("short.png", "network"), ("cut-chunked.png", "network"), ("cut-unsized.png", "busy")):
            with self.assertRaises(imgproxy.ImageError) as caught:
                self.cache.get(self.url(name))
            self.assertEqual((caught.exception.status, caught.exception.kind), (503, kind), name)
        self.assertEqual([p.name for p in self.folder.iterdir()], ["failed.json"])  # обрывки на диск не попали
        self.assertEqual(list(json.loads((self.folder / "failed.json").read_text("utf-8"))), [self.url("cut-unsized.png")])
        # обрывок, сохранённый раньше, не отдаётся — картинка скачивается заново
        url = self.url("raw.png")
        (self.folder / (imgproxy.ImageCache._name(url) + ".png")).write_bytes(BIG_PNG[:1000])
        self.assertEqual(self.cache.get(url), (PNG, "image/png"))
        self.assertEqual(Upstream.calls["/raw.png"], 1)

    def test_complete_pictures(self):
        avif = (16).to_bytes(4, "big") + b"ftypavif\x00\x00\x00\x00" + (12).to_bytes(4, "big") + b"mdatabcd"
        for data, ext in ((picgen.png_bytes(4, 4, bytes(64)), ".png"), (WEBP, ".webp"), (GIF, ".gif"),
                          (b"\xff\xd8\xff\xe0" + b"\x01" * 100 + b"\xff\xd9", ".jpg"), (avif, ".avif")):
            self.assertEqual(imgproxy.sniff(data)[1], ext)
            self.assertTrue(imgproxy.complete(data, ext), ext)
            self.assertFalse(imgproxy.complete(data[:len(data) * 2 // 3], ext), ext)

    def test_a_blocking_site_is_paused_as_a_whole(self):
        # 403, 451, «страница защиты» вместо картинки: сайт не пускает программу.
        for name in ("forbidden.webp", "legal.webp", "challenge.webp"):
            with self.assertRaises(imgproxy.ImageError) as caught:
                self.download(self.url(name))
            self.assertEqual((caught.exception.status, caught.exception.kind), (429, "blocked"), name)
        calls = []

        def download(url):
            calls.append(url)
            if "supremevalues.com" in url:
                raise imgproxy.ImageError(429, "не пускает", "blocked")
            return PNG, ("image/png", ".png")

        cache = imgproxy.ImageCache(self.folder, download=download, clock=self.clock)

        def status(url):
            try:
                cache.get(url)
            except imgproxy.ImageError as error:
                return error.status
            return 200

        # тысяча картинок защищённого сайта — один запрос к нему, остальные ждут паузу
        site = "https://supremevalues.com/mm2/items/"
        self.assertEqual({status(f"{site}{number}.webp") for number in range(1000)}, {429})
        self.assertEqual(status("https://www.supremevalues.com/x.webp"), 429)  # поддомен — тот же сайт
        self.assertEqual(len(calls), 1)
        self.assertEqual(status(self.url("ok.webp")), 200)  # StarPets пускает
        self.assertFalse((self.folder / "failed.json").exists())  # сами адреса не виноваты
        self.clock.now += imgproxy.BLOCK_PAUSE + 1
        self.assertEqual(status(site + "again.webp"), 429)
        self.assertEqual(len([url for url in calls if "supremevalues" in url]), 2)

    def test_one_download_then_disk(self):
        url, out = self.url("ok.webp"), []
        threads = [threading.Thread(target=lambda: out.append(self.cache.get(url))) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual((len(out), Upstream.calls["/ok.webp"]), (8, 1))
        again = imgproxy.ImageCache(self.folder, download=self.download)  # программу перезапустили
        self.assertEqual(again.get(url), (WEBP, "image/webp"))
        self.assertEqual(Upstream.calls["/ok.webp"], 1)

    def test_failures_are_remembered(self):
        for _ in range(3):
            self.assertEqual(self.status(self.url("missing.webp")), 404)
            self.assertEqual(self.status(self.url("busy.webp")), 503)
        self.assertEqual((Upstream.calls["/missing.webp"], Upstream.calls["/busy.webp"]), (1, 1))
        restarted = imgproxy.ImageCache(self.folder, download=self.download, clock=self.clock)
        with self.assertRaises(imgproxy.ImageError):
            restarted.get(self.url("missing.webp"))
        self.assertEqual(Upstream.calls["/missing.webp"], 1)  # и после перезапуска — по failed.json
        self.clock.now += imgproxy.BUSY_RETRY + 1  # занятый сайт — через 10 минут снова
        self.status(self.url("busy.webp"))
        self.status(self.url("missing.webp"))
        self.assertEqual((Upstream.calls["/missing.webp"], Upstream.calls["/busy.webp"]), (1, 2))
        self.clock.now += imgproxy.FAIL_RETRY  # «нет такой картинки» — через 6 часов
        self.status(self.url("missing.webp"))
        self.assertEqual(Upstream.calls["/missing.webp"], 2)
        failed = json.loads((self.folder / "failed.json").read_text("utf-8"))
        self.assertEqual(sorted(failed), [self.url("busy.webp"), self.url("missing.webp")])

    def test_no_network_answers_at_once(self):
        calls = []

        def offline(url):
            calls.append(url)
            raise imgproxy.ImageError(503, "нет связи", "network")

        cache = imgproxy.ImageCache(self.folder, download=offline, clock=self.clock)
        for number in range(10):
            with self.assertRaises(imgproxy.ImageError) as caught:
                cache.get(self.url(f"{number}.webp"))
            self.assertEqual(caught.exception.status, 503)
        self.assertEqual(len(calls), imgproxy.OFFLINE_AFTER)  # дальше — сразу «нет связи», без ожидания
        self.assertFalse((self.folder / "failed.json").exists())  # сами адреса не виноваты
        self.clock.now += imgproxy.OFFLINE_PAUSE + 1
        with self.assertRaises(imgproxy.ImageError):
            cache.get(self.url("again.webp"))
        self.assertEqual(len(calls), imgproxy.OFFLINE_AFTER + 1)

    def test_cache_size_is_limited(self):
        cache = imgproxy.ImageCache(self.folder, download=lambda url: (PNG[:-12] + url.encode() * 20 + IEND,
                                                                        ("image/png", ".png")))
        with mock.patch.object(imgproxy, "MAX_CACHE_BYTES", 3000):
            for number in range(40):
                cache.get(self.url(f"{number:02d}.png"))
        total = sum(p.stat().st_size for p in self.folder.iterdir())
        self.assertLessEqual(total, 3000)
        self.assertGreater(len(list(self.folder.iterdir())), 3)


class FeatureStoreTests(unittest.TestCase):
    SITE = "https://supremevalues.com/mm2/items/cowboy.webp"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "picfeat.json"
        self.store = imgproxy.FeatureStore(self.path)
        self.sources = {"vintages:Cowboy": {"site": self.SITE}}
        self.keys = {"vintages:Cowboy", "rares:Cowboy"}
        self.categories = set(parser.CATEGORY_TITLES)

    def update(self, body):
        return self.store.update(dict({"version": imgproxy.FEAT_VERSION}, **body), self.sources, self.keys, self.categories)

    def snapshot(self):
        return json.loads(self.store.snapshot_json())

    def test_only_pictures_of_known_items(self):
        saved = self.update({"feats": {
            "vintages:Cowboy": {"site": {"url": self.SITE, "f": "AAAA"}, "market": {"url": "https://x", "f": "BBBB"}},
            "godlies:Nope": {"site": {"url": self.SITE, "f": "CCCC"}},
        }})
        self.assertEqual(saved, 1)
        self.assertEqual(self.snapshot()["feats"], {"vintages:Cowboy": {"site": {"url": self.SITE, "f": "AAAA"}}})
        # картинка предмета сменилась — признаки старой не принимаются
        self.assertEqual(self.update({"feats": {"vintages:Cowboy": {"site": {"url": self.SITE + "?v2", "f": "DDDD"}}}}), 0)
        for bad in ("", "<script>", "A" * (imgproxy.MAX_FEAT_CHARS + 4), 5, None):
            self.assertEqual(self.update({"feats": {"vintages:Cowboy": {"site": {"url": self.SITE, "f": bad}}}}), 0, bad)

    def test_learned_pictures_and_banner_colours(self):
        for number in range(5):
            self.update({"learn": {"key": "rares:Cowboy", "f": f"L{number}AA"}})
        self.update({"learn": {"key": "godlies:Nope", "f": "AAAA"}})
        self.assertEqual(self.snapshot()["learned"], {"rares:Cowboy": ["L2AA", "L3AA", "L4AA"]})  # последние три
        self.update({"banner": {"bucket": "50", "category": "vintages"}})
        self.update({"banner": {"bucket": "50", "category": "vintages"}})
        self.update({"banner": {"bucket": "<b>", "category": "vintages"}})
        self.update({"banner": {"bucket": "rainbow", "category": "nonsense"}})
        self.assertEqual(self.snapshot()["banners"], {"50": {"vintages": 2}})
        on_disk = json.loads(self.path.read_text("utf-8"))  # сохранено (переживёт перезапуск)
        self.assertEqual(on_disk["learned"]["rares:Cowboy"][-1], "L4AA")
        self.assertEqual(imgproxy.FeatureStore(self.path).snapshot_json(), self.store.snapshot_json())

    def test_odd_requests_are_ignored(self):
        for body in ({"learn": {"key": ["x"], "f": "AAAA"}}, {"learn": {"key": {"a": 1}, "f": "AAAA"}},
                     {"banner": {"bucket": "10", "category": {"a": 1}}}, {"banner": {"bucket": ["10"], "category": "rares"}},
                     {"learn": ["junk", None, 5]}, {"banner": "50"}, {"feats": ["x"]}, {"feats": {"vintages:Cowboy": "x"}}):
            self.assertEqual(self.update(body), 0, body)
        self.assertFalse(self.path.exists())

    def test_many_learned_pictures_in_one_request(self):
        writes, real = [], imgproxy._atomic_write
        with mock.patch.object(imgproxy, "_atomic_write", side_effect=lambda path, data: (writes.append(path), real(path, data))):
            saved = self.update({
                "learn": [{"key": "rares:Cowboy", "f": f"L{n}AA"} for n in range(5)]
                + [{"key": "vintages:Cowboy", "f": "VVVV"}, {"key": "godlies:Nope", "f": "AAAA"}],
                "banner": [{"bucket": "50", "category": "vintages"}] * 3 + [{"bucket": "rainbow", "category": "nonsense"}],
            })
        self.assertEqual((saved, len(writes)), (9, 1))  # всё сохранено, файл переписан один раз
        snap = self.snapshot()
        self.assertEqual(snap["learned"], {"rares:Cowboy": ["L2AA", "L3AA", "L4AA"], "vintages:Cowboy": ["VVVV"]})
        self.assertEqual(snap["banners"], {"50": {"vintages": 3}})
        many = [{"key": "vintages:Cowboy", "f": f"W{n}AA"} for n in range(imgproxy.MAX_LEARN_POST + 5)]
        self.assertEqual(self.update({"learn": many}), imgproxy.MAX_LEARN_POST)  # не больше записей за раз
        self.assertEqual(self.snapshot()["learned"]["vintages:Cowboy"][-1], f"W{imgproxy.MAX_LEARN_POST - 1}AA")

    def test_other_version_or_broken_file(self):
        with self.assertRaises(ValueError):
            self.store.update({"version": 99}, self.sources, self.keys, self.categories)
        self.path.write_text("{broken", encoding="utf-8")
        fresh = json.loads(imgproxy.FeatureStore(self.path).snapshot_json({"sources": {}}))
        self.assertEqual(fresh, {"version": imgproxy.FEAT_VERSION, "feats": {}, "learned": {}, "banners": {}, "sources": {}})

    def test_moved_with_the_prices_from_the_old_data_folder(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        exe_dir = Path(tmp.name).resolve() / "exe"
        old, local = exe_dir / "data", Path(tmp.name) / "Local"
        (old / "images").mkdir(parents=True)
        for name, text in (("values.json", "{}"), ("picfeat.json", "{}"), ("images/0123.png", "png")):
            (old / name).write_text(text, encoding="utf-8")
        with mock.patch.object(paths, "FROZEN", True), \
                mock.patch.object(paths.sys, "executable", str(exe_dir / "MM2Values.exe")), \
                mock.patch.dict(paths.os.environ, {"LOCALAPPDATA": str(local)}):
            folder = paths.migrate_old_data()
        self.assertEqual(folder, local / "MM2Values" / "data")
        self.assertTrue((folder / "picfeat.json").is_file())
        self.assertTrue((folder / "images" / "0123.png").is_file())
        self.assertFalse(old.exists())


class FakeMonitor:
    """Площадка без сети: цена и картинка StarPets у некоторых предметов."""

    version = 1

    def __init__(self, images):
        self.images = images

    def annotate(self, items):
        for item in items:
            key = markets.item_id(item)
            item["market"] = {"starpets": {"price": 2.5, "currency": "USD", "fee": 0.2, "stock": 4}}
            if not item.get("image") and key in self.images:
                item["image_market"] = self.images[key]
        return items

    def configs(self):
        return [{"id": "starpets", "title": "StarPets", "fee": 0.2}]

    def status_list(self):
        return []

    def poll_now(self):
        pass


def stock():
    """Предметы для проверок: два Cowboy (ружьё и нож), секретный, набор, картинка с чужого сайта."""
    def item(name, category, value="10", image=""):
        made = parser.make_item(name, category, {"value": value, "demand": "2", "rarity": "3"})
        made["image"] = image
        return made

    return [
        item("Cowboy", "vintages", image="https://supremevalues.com/mm2/items/cowboy.webp"),
        item("Cowboy", "rares"),  # картинка только у StarPets
        item("Harvester", "ancients", image="https://evil.example/harvester.png"),
        item("Seer", "godlies", image="https://supremevalues.com/mm2/items/seer.webp"),
        item("Batwing", "godlies", value="1,000,000", image="https://supremevalues.com/mm2/items/batwing.webp"),
        item("Gun Set", "sets", image="https://supremevalues.com/mm2/items/seer.webp"),
    ]


def save_stock(path, items):
    data = store.empty_data()
    for category in sorted({i["category"] for i in items}):
        store.replace_category(data, category, [i for i in items if i["category"] == category], "site")
    store.save(data, path)


class PicServerTests(unittest.TestCase):
    MARKET = {"rares:Cowboy": "https://cdn.starpets.gg/MM/110x110/cowboy_k.webp",
              "ancients:Harvester": "https://cdn.starpets.gg/MM/110x110/harv.webp"}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name) / "values.json"
        save_stock(self.data, stock())
        self.httpd = server.make_server(port=0, data_path=self.data, update=lambda log: None,
                                        monitor=FakeMonitor(self.MARKET))
        self.downloads = []

        def download(url):
            self.downloads.append(url)
            return PNG, ("image/png", ".png")

        handler = self.httpd.RequestHandlerClass
        self.assertEqual(handler.images.folder, Path(tmp.name) / "images")  # рядом с ценами
        self.assertEqual(handler.features.path, Path(tmp.name) / "picfeat.json")
        handler.images = imgproxy.ImageCache(handler.images.folder, download=download)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.port = self.httpd.server_address[1]

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        all_headers = {"Host": f"127.0.0.1:{self.port}"}
        if body is not None:
            all_headers["Content-Type"] = "application/json"
            body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        all_headers.update(headers or {})
        connection.request(method, path, body=body, headers=all_headers)
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, dict(response.getheaders()), payload

    def post(self, path, body, headers=None):
        status, _, payload = self.request("POST", path, body, headers)
        return status, json.loads(payload)


class ItemImageEndpointTests(PicServerTests):
    def test_picture_of_a_known_item_only(self):
        status, headers, body = self.request("GET", "/api/itemimg?key=vintages%3ACowboy&src=site")
        self.assertEqual((status, body, headers["Content-Type"]), (200, PNG, "image/png"))
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        # адрес — ключ предмета: из кэша браузера после смены картинки пришла бы старая
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("sandbox", headers["Content-Security-Policy"])
        status, _, body = self.request("GET", "/api/itemimg?key=rares%3ACowboy&src=market")
        self.assertEqual((status, body), (200, PNG))
        self.assertEqual(self.downloads, ["https://supremevalues.com/mm2/items/cowboy.webp", self.MARKET["rares:Cowboy"]])
        for query in ("key=rares%3ACowboy&src=site",            # своей картинки нет
                      "key=ancients%3AHarvester&src=site",      # картинка с чужого сайта
                      "key=godlies%3ABatwing&src=site",         # секретный предмет
                      "key=sets%3AGun%20Set&src=site",          # набор — не плитка инвентаря
                      "key=godlies%3ANope", "src=site&key=https%3A%2F%2Fevil.example%2Fx.png"):
            self.assertEqual(self.request("GET", "/api/itemimg?" + query)[0], 404, query)
        self.assertEqual(len(self.downloads), 2)
        # StarPets-картинка Harvester (у его своей адрес чужой) — можно
        self.assertEqual(self.request("GET", "/api/itemimg?key=ancients%3AHarvester&src=market")[0], 200)

    def test_blocking_site_answers_at_once(self):
        calls = []

        def blocked(url):
            calls.append(url)
            raise imgproxy.ImageError(429, "не пускает", "blocked")

        handler = self.httpd.RequestHandlerClass
        handler.images = imgproxy.ImageCache(handler.images.folder, download=blocked)
        for key in ("vintages%3ACowboy", "godlies%3ASeer"):
            status, _, body = self.request("GET", f"/api/itemimg?key={key}&src=site")
            self.assertEqual(status, 429, body)
            self.assertIn("не пускает", json.loads(body)["error"])
        self.assertEqual(len(calls), 1)

    def test_foreign_pages_and_hosts_refused(self):
        for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Host": "evil.example"}):
            for path in ("/api/itemimg?key=vintages%3ACowboy&src=site", "/api/picfeat"):
                self.assertEqual(self.request("GET", path, headers=headers)[0], 403, (path, headers))
        status, payload = self.post("/api/picfeat", {"version": imgproxy.FEAT_VERSION}, {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(self.downloads, [])

    def test_features_saved_once_for_the_current_picture(self):
        status, _, body = self.request("GET", "/api/picfeat")
        snap = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(snap["sources"], {
            "vintages:Cowboy": {"site": "https://supremevalues.com/mm2/items/cowboy.webp"},
            "rares:Cowboy": {"market": self.MARKET["rares:Cowboy"]},
            "ancients:Harvester": {"market": self.MARKET["ancients:Harvester"]},
            "godlies:Seer": {"site": "https://supremevalues.com/mm2/items/seer.webp"},
        })
        feats = {"vintages:Cowboy": {"site": {"url": snap["sources"]["vintages:Cowboy"]["site"], "f": "AAAA"}},
                 "godlies:Seer": {"site": {"url": "https://supremevalues.com/other.webp", "f": "BBBB"}}}
        status, payload = self.post("/api/picfeat", {"version": imgproxy.FEAT_VERSION, "feats": feats,
                                                     "learn": {"key": "rares:Cowboy", "f": "CCCC"},
                                                     "banner": {"bucket": "210", "category": "rares"}})
        self.assertEqual((status, payload), (200, {"saved": 3}))
        snap = json.loads(self.request("GET", "/api/picfeat")[2])
        self.assertEqual(list(snap["feats"]), ["vintages:Cowboy"])
        self.assertEqual((snap["learned"], snap["banners"]), ({"rares:Cowboy": ["CCCC"]}, {"210": {"rares": 1}}))
        self.assertEqual(self.post("/api/picfeat", {"version": 0})[0], 409)
        # странные поля — ответ, а не обрыв связи
        for odd in ({"learn": {"key": ["x"], "f": "AAAA"}}, {"banner": {"bucket": "10", "category": {"a": 1}}}):
            self.assertEqual(self.post("/api/picfeat", dict(odd, version=imgproxy.FEAT_VERSION)), (200, {"saved": 0}))
        with mock.patch.object(self.httpd.RequestHandlerClass.features, "update", side_effect=TypeError("странно")):
            self.assertEqual(self.post("/api/picfeat", {"version": imgproxy.FEAT_VERSION})[0], 400)


class CalcPicturesTests(PicServerTests):
    def tile(self, name, box, qty=1, badge=None, bg=None, **extra):
        tile = {"name": name, "text": name or "", "score": 1.0 if name else 0.0, "partial": False, "qty": qty,
                "qty_from_badge": badge is not None, "box": box, "name_box": [box[0] + 20, box[1] + 145, 90, 20],
                "badge_box": badge, "bg": bg, "clipped": False, "qty_uncertain": False}
        tile.update(extra)
        return tile

    def test_tiles_of_each_screenshot_for_the_browser(self):
        tiles = [self.tile("Seer", [20, 30, 130, 170], 3, badge=[110, 40, 30, 16], bg=[220, 60, 180]),
                 self.tile("Cowboy", [159, 30, 130, 170], bg=[60, 60, 60]),
                 self.tile(None, [298, 30, 130, 170], 2, badge=[388, 40, 30, 16])]
        reads = iter([({"width": 800, "height": 600, "words": []}, tiles), ocr.OcrError("не вышло")])

        def read_inventory(raw, matcher):
            result = next(reads)
            if isinstance(result, Exception):
                raise result
            return result

        with mock.patch.object(ocr, "read_inventory", side_effect=read_inventory):
            status, payload = self.post("/api/calc", {"images": ["data:image/png;base64,AAAA", "QUFB"], "pictures": True})
        self.assertEqual(status, 200)
        first, second = payload["shots"]
        self.assertEqual((first["index"], first["width"], first["height"], first["mode"]), (0, 800, 600, "tiles"))
        self.assertEqual((second["index"], second["mode"], second["tiles"]), (1, "error", []))
        seer, cowboy, nameless = first["tiles"]
        self.assertEqual((seer["id"], seer["box"], seer["badge_box"], seer["name"], seer["qty"], seer["qty_from_badge"]),
                         ("0:0", [20, 30, 130, 170], [110, 40, 30, 16], "Seer", 3, True))
        self.assertEqual((seer["key"], seer["sure"], seer["candidates"], seer["bg"]),
                         ("godlies:Seer", True, ["godlies:Seer"], [220, 60, 180]))
        self.assertEqual(sorted(cowboy["candidates"]), ["rares:Cowboy", "vintages:Cowboy"])  # серая полоса не подсказала
        self.assertEqual((cowboy["key"] in cowboy["candidates"], cowboy["sure"]), (True, False))
        self.assertEqual((nameless["name"], nameless["key"], nameless["candidates"], nameless["qty"]), (None, None, [], 2))
        rows = {row["key"]: row for row in payload["items"]}
        self.assertEqual(rows["godlies:Seer"]["tiles"], [{"id": "0:0", "qty": 3}])
        self.assertEqual((rows["godlies:Seer"]["via"], rows["godlies:Seer"]["tile_qty"]), ("name", 3))
        other = ({"rares:Cowboy", "vintages:Cowboy"} - {cowboy["key"]}).pop()
        self.assertTrue(rows[cowboy["key"]]["ambiguous"])
        self.assertEqual([alt["key"] for alt in rows[cowboy["key"]]["alternatives"]], [other])
        self.assertEqual(rows[cowboy["key"]]["tiles"], [{"id": "0:1", "qty": 1}])
        self.assertEqual(payload["unknown_tiles"], 1)
        self.assertTrue(payload["ocr_errors"])

    def test_text_screenshots_report_their_rows(self):
        # Скриншоты, прочитанные как текст, считаются каждый отдельно: «x2» в конце первого
        # относится к нему, а не к первой строке второго; браузер знает, что нашлось на каждом.
        def words(*lines):
            return {"width": 800, "height": 600, "words": [
                {"text": text, "x": 0, "y": 20 * number, "w": 60, "h": 15, "pass": "white", "line": number}
                for number, text in enumerate(lines)]}

        reads = iter([(words("Seerr", "Harvester", "x2"), [self.tile(None, [20, 30, 130, 170], 3, badge=[110, 40, 30, 16])]),
                      (words("Seer"), [])])
        with mock.patch.object(ocr, "read_inventory", side_effect=lambda raw, matcher: next(reads)):
            status, payload = self.post("/api/calc", {"images": ["QUFB", "QUFC"], "pictures": True, "text": "Seer x5"})
        self.assertEqual(status, 200)
        first, second = payload["shots"]
        self.assertEqual((first["mode"], first["text_items"], second["mode"]), ("text", 2, "text"))
        self.assertEqual(first["text_rows"], [{"key": "godlies:Seer", "qty": 1, "score": 0.89, "seen": ["Seerr"]},
                                              {"key": "ancients:Harvester", "qty": 2, "score": 1.0, "seen": ["Harvester"]}])
        self.assertEqual(second["text_rows"], [{"key": "godlies:Seer", "qty": 1, "score": 1.0, "seen": ["Seer"]}])
        self.assertEqual(len(first["tiles"]), 1)  # плитка без названия — браузеру
        rows = {row["key"]: row for row in payload["items"]}
        self.assertEqual({key: (row["qty"], row["score"]) for key, row in rows.items()},
                         {"godlies:Seer": (7, 0.89), "ancients:Harvester": (2, 1.0)})
        self.assertEqual(rows["godlies:Seer"]["seen"], ["Seer", "Seerr", "Seer"])
        self.assertEqual(payload["ocr_lines"], 4)

    def test_screenshot_with_no_tiles_still_goes_to_the_browser(self):
        with mock.patch.object(ocr, "read_inventory", side_effect=ocr.OcrError("только в Windows")):
            status, payload = self.post("/api/calc", {"images": ["QUFB"], "pictures": True})
            self.assertEqual(status, 200)
            self.assertEqual((payload["items"], payload["shots"][0]["mode"]), ([], "error"))
            self.assertEqual(self.post("/api/calc", {"images": ["QUFB"]})[0], 400)  # без браузерной части — как раньше

    def test_picked_items_get_prices(self):
        picked = [{"key": "godlies:Seer", "qty": 3, "via": "pic"},
                  {"key": "godlies:Seer", "qty": "2", "via": "user"},
                  {"key": "rares:Cowboy", "qty": 10 ** 9, "via": "<b>"},
                  {"key": "ancients:Harvester", "qty": -5, "via": "user"},
                  {"key": "godlies:Batwing", "qty": 1},     # секретный — не продаётся
                  {"key": "godlies:Nope", "qty": 1}, {"key": 5}, "junk", None]
        status, payload = self.post("/api/calc", {"text": "Seer", "picked": picked})
        self.assertEqual(status, 200)
        rows = {row["key"]: row for row in payload["items"]}
        self.assertEqual({key: row["qty"] for key, row in rows.items()},
                         {"godlies:Seer": 6, "rares:Cowboy": 9999, "ancients:Harvester": 1})
        self.assertEqual(rows["rares:Cowboy"]["via"], "pic")
        self.assertEqual(rows["ancients:Harvester"]["via"], "user")
        self.assertEqual(rows["rares:Cowboy"]["prices"]["starpets"]["price"], 2.5)
        status, payload = self.post("/api/calc", {"picked": [{"key": "rares:Cowboy", "qty": 2}]})  # без текста
        self.assertEqual((status, [(row["name"], row["qty"]) for row in payload["items"]]), (200, [("Cowboy", 2)]))
        self.assertEqual(self.post("/api/calc", {"picked": "rares:Cowboy"})[0], 400)
        many = [{"key": "rares:Cowboy", "qty": 1}] * 2000
        self.assertEqual(self.post("/api/calc", {"picked": many})[1]["items"][0]["qty"], 500)  # не больше 500 записей


class BadgeEndpointTests(PicServerTests):
    def test_badges_read_again(self):
        boxes = [[20, 30, 130, 170], [159, 30, 130, 170], [298, 30, 130, 170], [1, 2], [437, 30, 3, 170]]
        seen = {}

        def recognize_regions(raw, regions, **kwargs):
            seen["raw"], seen["regions"] = raw, regions
            words = [{"text": "x12", "region": 0, "pass": p, "line": p, "x": 0} for p in ("white", "grayinv")]
            areas = [{"region": 1, "ink": [251, 40, 24, 15]}]  # значок на месте, но не прочитан
            return {"words": words, "areas": areas}

        real = tileocr.apply_region_badges
        with mock.patch.object(ocr, "recognize_regions", side_effect=recognize_regions), \
                mock.patch.object(tileocr, "apply_region_badges", side_effect=real) as apply:
            status, payload = self.post("/api/calc/badges", {"shots": [
                {"index": 1, "image": "data:image/png;base64," + base64.b64encode(b"shot").decode(), "boxes": boxes}]})
        self.assertEqual(status, 200)
        self.assertEqual(seen["raw"], b"shot")
        self.assertEqual(len(seen["regions"]), 3)  # неверные места не читаются
        self.assertEqual(apply.call_args[0][3], [{"region": 1, "ink": [251, 40, 24, 15]}])  # с «areas»
        shot = payload["shots"][0]
        self.assertEqual((shot["index"], shot["read"]), (1, True))
        self.assertEqual([(t["qty"], t["qty_from_badge"], t["qty_uncertain"]) for t in shot["tiles"]],
                         [(12, True, False), (1, False, True), (1, False, False), (1, False, False), (1, False, False)])

    def test_errors_are_reported_per_screenshot(self):
        with mock.patch.object(ocr, "recognize_regions", side_effect=ocr.OcrError("только в Windows")):
            status, payload = self.post("/api/calc/badges", {"shots": [
                {"index": 0, "image": "QUFB", "boxes": [[20, 30, 130, 170]]},
                {"index": 1, "image": "@@@", "boxes": []}, "junk"]})
        self.assertEqual(status, 200)
        self.assertEqual([(s["index"], s["read"], len(s["tiles"])) for s in payload["shots"]], [(0, False, 1), (1, False, 0)])
        self.assertEqual(payload["ocr_errors"], ["скриншот 1: только в Windows"])
        too_many = {"shots": [{"index": n, "image": "QUFB", "boxes": []} for n in range(server.MAX_IMAGES + 1)]}
        self.assertEqual(self.post("/api/calc/badges", too_many)[0], 400)

    def test_size_limits(self):
        body = {"shots": [{"index": 0, "image": "A" * 5000, "boxes": []}]}
        with mock.patch.object(server, "MAX_BODY", 1000):
            self.assertEqual(self.post("/api/calc/badges", body)[0], 200)  # скриншоты — больший предел
            self.assertEqual(self.post("/api/picfeat", {"version": 1, "f": "A" * 5000})[0], 413)
        with mock.patch.object(server, "MAX_CALC_BODY", 1000):
            status, payload = self.post("/api/calc/badges", body)
        self.assertEqual(status, 413)
        self.assertIn("Скриншоты", payload["error"])


if __name__ == "__main__":
    unittest.main()
