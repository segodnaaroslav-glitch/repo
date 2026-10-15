"""Калькулятор со скриншотами в настоящем браузере (скрытый Chromium/Edge): программа целиком,
распознавание текста и площадки — подделки, картинки предметов — нарисованные tests/picgen.py."""

import base64
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from supreme import calc as calc_module, cdp, fetcher, imgproxy, markets, ocr, parser, server, store
from tests import picgen
from tests.test_imgproxy import FakeMonitor

BROWSER = os.environ.get("MM2_TEST_BROWSER") or fetcher.find_system_browser() or (
    "/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None
)
WIDTH = 100

# Скриншот A: распознавание текста прочитало часть плиток (у Elderwood Scythe и Luger название не
# прочиталось, Icebreaker оно не нашло совсем, у двух Cowboy цвет полосы не подсказал предмет).
SHOT_A = [("ancients:Harvester", "x3"), ("godlies:Elderwood Scythe", None), ("godlies:Seer", "x12"),
          ("chromas:Chroma Seer", None), ("godlies:Icebreaker", "x2"), ("vintages:Cowboy", "x40"),
          ("rares:Cowboy", None), None, ("godlies:Fang", "x7"), ("commons:Luger", None)]
UNREAD_NAMES = {"godlies:Elderwood Scythe", "commons:Luger"}
OCR_MISSED = {"godlies:Icebreaker"}
COWBOY_BG = {"vintages:Cowboy": [90, 90, 90], "rares:Cowboy": None}
# Скриншот B: распознавание не нашло ни одной плитки; те же Fang и Icebreaker (одна стопка
# с A — считаются один раз), значок Icebreaker здесь не прочитался.
SHOT_B = [("godlies:Fang", "x7"), ("godlies:Icebreaker", "x2")]
UNREADABLE_BADGES = {("B", "godlies:Icebreaker")}

SITE = "https://supremevalues.com/mm2/items/"
MARKET = {"rares:Cowboy": "https://cdn.starpets.gg/MM/110x110/cowboy_k.webp"}  # у ножа Cowboy — только StarPets
NO_PICTURE = {"commons:Luger"}  # картинки нет нигде: узнать её может только пользователь


def stock():
    items = []
    for key, (name, category, *_rest) in picgen.ITEMS.items():
        item = parser.make_item(name, category, {"value": "10", "demand": "2", "rarity": "3"})
        if key not in MARKET and key not in NO_PICTURE:
            item["image"] = SITE + name.lower().replace(" ", "-") + "-" + category + ".webp"
        items.append(item)
    return items


def shot(cells, seed):
    canvas, truth = picgen.grid(cells, width=WIDTH, seed=seed)
    return canvas.png(alpha=False), truth


def ocr_tiles(truth, unread=UNREAD_NAMES, missed=OCR_MISSED):
    """Плитки, как их вернул бы tileocr.recognize (место приблизительное)."""
    tiles = []
    for place in truth:
        key = place["key"]
        if key in missed:
            continue
        name = None if key in unread else picgen.ITEMS[key][0]
        x0, top, (b0, b1) = place["x0"], place["icon_top"], place["banner"]
        badge = place["badge"]
        tiles.append({
            "name": name, "text": name or "", "score": 1.0 if name else 0.0, "partial": False,
            "qty": int(badge[1:]) if badge else 1, "qty_from_badge": bool(badge),
            "box": [x0 - 1, top - 2, WIDTH, round(1.31 * WIDTH)],
            "name_box": [x0 + 20, b0 + 5, WIDTH - 40, b1 - b0 - 10] if name else None,
            "badge_box": [x0 + 62, top + 9, 30, 12] if badge else None,
            "bg": COWBOY_BG.get(key, list(picgen.ITEMS[key][4])), "clipped": False, "qty_uncertain": False,
        })
    return tiles


def text_words(*lines):
    """Слова распознавания: каждая строка — одним словом (для скриншотов, прочитанных как текст)."""
    return {"width": 0, "height": 0, "words": [{"text": text, "x": 5, "y": 5 + 20 * number, "w": 40, "h": 12,
                                                "pass": "white", "line": number} for number, text in enumerate(lines)]}


def cut_bottom(canvas, height):
    """Скриншот, обрезанный снизу (инвентарь прокручивается — нижний ряд виден не целиком)."""
    cut = picgen.Canvas(canvas.width, height)
    cut.px = canvas.px[:canvas.width * height * 4]
    return cut.png(alpha=False)


@unittest.skipUnless(BROWSER, "нет Edge/Chrome/Chromium")
def _remove_folder(path):
    """Убрать папку проверки. Запрос страницы, начатый до остановки сервера, может дописать
    файл (картинку, failed.json) уже во время уборки — тогда ещё раз, чуть позже."""
    for _ in range(10):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.3)
    shutil.rmtree(path, ignore_errors=True)


class CalcPicturesBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.png_a, cls.truth_a = shot(SHOT_A, 11)
        cls.png_b, cls.truth_b = shot(SHOT_B, 12)
        cls.pictures = {}
        for key, (name, category, *_rest) in picgen.ITEMS.items():
            url = MARKET.get(key) or SITE + name.lower().replace(" ", "-") + "-" + category + ".webp"
            cls.pictures[url] = picgen.reference(key).png()
        cls.browser = cdp.Browser(BROWSER)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(_remove_folder, self.folder)
        data = store.empty_data()
        items = stock()
        for category in sorted({i["category"] for i in items}):
            store.replace_category(data, category, [i for i in items if i["category"] == category], "site")
        store.save(data, self.folder / "values.json")
        self.httpd = server.make_server(port=0, data_path=self.folder / "values.json", update=lambda log: None,
                                        monitor=FakeMonitor(MARKET))
        self.handler = self.httpd.RequestHandlerClass
        self.downloads, self.offline = [], False
        self.missing, self.corrupt, self.busy, self.site_blocked, self.site_down = set(), set(), set(), False, False
        self.gate = None  # threading.Event: картинки отдаются, только когда он открыт (медленная сеть)
        self.badge_errors = set()  # скриншоты, значки которых распознавание не прочитало (ошибка)
        self.inventories, self.truths = {}, {}  # скриншоты отдельных проверок: png -> ответ распознавания
        self.handler.images = imgproxy.ImageCache(self.folder / "images", download=self.download)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        for patch in (mock.patch.object(ocr, "read_inventory", side_effect=self.read_inventory),
                      mock.patch.object(ocr, "recognize_regions", side_effect=self.recognize_regions),
                      # бережность к сайтам проверяется в test_imgproxy; здесь — без ожидания
                      mock.patch.dict(imgproxy.PACE, {site: (parallel, 0.0) for site, (parallel, _) in imgproxy.PACE.items()})):
            patch.start()
            self.addCleanup(patch.stop)
        self.badge_calls = []
        self.browser.open(f"http://127.0.0.1:{self.httpd.server_address[1]}/", settle=0.2)
        self.wait("state.data.items.length > 0 && state.dataVersion !== null")

    # --- подделки ---

    def download(self, url):
        self.downloads.append(url)
        if self.gate is not None:
            self.gate.wait(30)
        if self.offline or (self.site_down and url.startswith(SITE)):
            raise imgproxy.ImageError(503, "нет связи", "network")
        if self.site_blocked and url.startswith(SITE):
            raise imgproxy.ImageError(429, "сайт не пускает программу", "blocked")
        if url in self.busy:
            raise imgproxy.ImageError(502, "сайт занят", "busy")
        if url not in self.pictures or url in self.missing:
            raise imgproxy.ImageError(404, "нет картинки")
        if url in self.corrupt:  # начало и конец PNG на месте, внутри — мусор: браузер её не откроет
            return self.pictures[url][:40] + b"\x13" * 200 + self.pictures[url][-12:], ("image/png", ".png")
        return self.pictures[url], ("image/png", ".png")

    def read_inventory(self, raw, matcher):
        if raw in self.inventories:
            words, tiles = self.inventories[raw]
            return words, [dict(tile) for tile in tiles]
        size = {"width": 0, "height": 0, "words": []}
        if raw == self.png_a:
            return size, ocr_tiles(self.truth_a)
        return size, []  # B: плиток не нашлось

    def recognize_regions(self, raw, regions, **kwargs):
        name, truth = self.truths.get(raw) or (("A", self.truth_a) if raw == self.png_a else ("B", self.truth_b))
        self.badge_calls.append((name, len(regions)))
        if name in self.badge_errors:
            raise ocr.OcrError("распознавание не ответило вовремя")
        words, areas = [], []
        for number, (x, y, w, h) in enumerate(regions):
            place = next((t for t in truth if t["x0"] <= x + w / 2 <= t["x1"] and abs(y - t["icon_top"]) < 20), None)
            if not place or not place["badge"]:
                continue
            areas.append({"region": number, "ink": [place["x0"] + 62, place["icon_top"] + 9, 30, 12]})
            if (name, place["key"]) not in UNREADABLE_BADGES:
                words += [{"text": place["badge"], "region": number, "pass": p, "line": p, "x": 0}
                          for p in ("white", "grayinv")]
        return {"words": words, "areas": areas}

    # --- страница ---

    def js(self, expression):
        return self.browser.evaluate(f"(async () => {{ {expression} }})()")

    def wait(self, condition, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.browser.evaluate(f"Boolean({condition})"):
                return
            time.sleep(0.1)
        self.fail(f"не дождался: {condition}")

    def add_files(self, *pngs):
        shots = json.dumps([base64.b64encode(png).decode() for png in pngs])
        return self.js(f"""
            $("calcClear").click();
            const files = {shots}.map((b64, i) => new File([Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))],
                                                            `shot${{i}}.png`, {{ type: "image/png" }}));
            addCalcFiles(files);
            while (calc.images.some((image) => !image.data)) await new Promise((r) => setTimeout(r, 20));
            return true;
        """)

    def run_calc(self, *pngs, wait=True):
        """Скриншоты -> «Посчитать». wait=False — не ждать сравнения по картинкам (строки уже показаны)."""
        self.add_files(*pngs)
        return self.js(f"await runCalc(); {'await PicTiles.idle();' if wait else ''} return true;")

    def snapshot(self):
        return self.js("""
            const rows = [...$("calcBody").children].map((tr, i) => ({
              key: calc.items[i].key, qty: calc.items[i].qty, via: calc.items[i].via || null,
              badges: [...tr.querySelectorAll(".badge")].map((b) => b.textContent),
              thumb: Boolean(tr.querySelector("img.tile-thumb")),
              variant: Boolean(tr.querySelector("select.variant")),
            }));
            const unsure = [...document.querySelectorAll("#calcPicTiles .pic-tile")].map((row) => ({
              text: row.textContent, options: [...row.querySelectorAll("select option")].map((o) => o.value),
              thumb: Boolean(row.querySelector("img.tile-thumb")),
            }));
            return { rows, unsure, status: ($("calcPicStatus") || {}).textContent || "",
                     calcStatus: $("calcStatus").textContent };
        """)

    def rows(self, snap):
        return {row["key"]: row for row in snap["rows"]}

    def learned(self, key, timeout=10):
        """Картинка из игры, выученная для предмета (браузер сохраняет её в фоне)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            learned = json.loads(self.handler.features.snapshot_json())["learned"]
            if key in learned:
                return learned[key]
            time.sleep(0.1)
        return None

    def choose_other(self, index, text):
        self.js(f"""
            const row = document.querySelectorAll("#calcPicTiles .pic-tile")[{index}];
            const select = row.querySelector("select");
            select.value = "__other";
            select.dispatchEvent(new Event("change"));
            const input = row.querySelector("input.pic-other");
            input.value = {json.dumps(text)};
            input.dispatchEvent(new Event("change"));
        """)

    # --- проверки ---

    def watch_posts(self):
        """Запросы страницы к серверу: [(адрес, тело)] — в window.seenPosts."""
        self.js("""
            window.seenPosts = [];
            const real = window.postJson;
            window.postJson = (url, body) => { seenPosts.push([url, body]); return real(url, body); };
            return true;
        """)

    def test_pictures_decide_variants_and_nameless_tiles(self):
        self.watch_posts()
        self.run_calc(self.png_a, self.png_b)
        # картинки из игры (плитки с точно прочитанным названием) — на сервер одним запросом
        learn = self.js('return seenPosts.filter(([url, body]) => url === "/api/picfeat" && body.learn && body.learn.length)'
                        '.map(([url, body]) => body.learn.map((entry) => entry.key));')
        self.assertEqual(len(learn), 1, learn)
        self.assertGreaterEqual(len(learn[0]), 3, learn)
        snap = self.snapshot()
        rows = self.rows(snap)
        self.assertEqual({key: row["qty"] for key, row in rows.items()}, {
            "ancients:Harvester": 3, "godlies:Seer": 12, "chromas:Chroma Seer": 1, "godlies:Fang": 7,
            "godlies:Elderwood Scythe": 1,   # название не прочиталось — узнан по картинке
            "godlies:Icebreaker": 2,         # распознавание текста плитку не нашло, «x2» прочитано вторым проходом
            "vintages:Cowboy": 40, "rares:Cowboy": 1,  # одно название — вариант выбран по картинке
        }, snap)
        for key in ("godlies:Elderwood Scythe", "godlies:Icebreaker", "vintages:Cowboy", "rares:Cowboy"):
            self.assertIn("по картинке", rows[key]["badges"], key)
            self.assertTrue(rows[key]["thumb"], key)
            self.assertNotIn("проверьте вариант", rows[key]["badges"], key)
        self.assertNotIn("по картинке", rows["ancients:Harvester"]["badges"])
        self.assertNotIn("проверьте количество", rows["godlies:Icebreaker"]["badges"])  # прочитан на скриншоте A
        self.assertEqual([call[0] for call in self.badge_calls], ["A", "B"])
        # Luger нет ни на сайте, ни на StarPets: решает пользователь
        self.assertEqual(len(snap["unsure"]), 1, snap["unsure"])
        self.assertTrue(snap["unsure"][0]["thumb"])
        self.assertEqual(snap["unsure"][0]["options"][-2:], ["__other", "__skip"])
        self.assertNotIn("commons:Luger", snap["unsure"][0]["options"])
        self.assertIn("узнано плиток по картинке: 4", snap["status"])  # с B — Fang и Icebreaker
        self.assertIn("вариант выбран по картинке: 2", snap["status"])
        self.assertIn("не уверен: 1", snap["status"])

        self.choose_other(0, "Luger")
        self.wait('calc.items.some((item) => item.key === "commons:Luger")')
        self.wait('document.querySelectorAll("#calcPicTiles .pic-tile").length === 0')
        luger = self.rows(self.snapshot())["commons:Luger"]
        self.assertEqual((luger["qty"], luger["via"]), (1, "user"))
        self.assertTrue(self.learned("commons:Luger"))  # выбор пользователя запомнен
        saved = json.loads(self.handler.features.snapshot_json())
        self.assertEqual(len(saved["feats"]), len(picgen.ITEMS) - len(NO_PICTURE))

        # Второй расчёт: картинки не скачиваются заново, Luger узнаётся по выученной картинке из игры.
        downloads = len(self.downloads)
        self.browser.open(f"http://127.0.0.1:{self.httpd.server_address[1]}/", settle=0.2)
        self.wait("state.data.items.length > 0 && state.dataVersion !== null")
        self.run_calc(self.png_a, self.png_b)
        snap = self.snapshot()
        rows = self.rows(snap)
        self.assertEqual(len(self.downloads), downloads)
        self.assertEqual(snap["unsure"], [])
        self.assertEqual((rows["commons:Luger"]["qty"], rows["commons:Luger"]["badges"][:1]), (1, ["по картинке"]))
        self.assertEqual(rows["vintages:Cowboy"]["qty"], 40)

    def test_without_pictures_names_work_as_before(self):
        self.offline = True
        self.run_calc(self.png_a, self.png_b)
        snap = self.snapshot()
        rows = self.rows(snap)
        cowboy = next(key for key in rows if key.endswith(":Cowboy"))
        self.assertEqual({key: row["qty"] for key, row in rows.items()}, {
            "ancients:Harvester": 3, "godlies:Seer": 12, "chromas:Chroma Seer": 1, "godlies:Fang": 7, cowboy: 41})
        self.assertIn("проверьте вариант", rows[cowboy]["badges"])
        self.assertTrue(rows[cowboy]["variant"])
        self.assertFalse(any("по картинке" in row["badges"] for row in snap["rows"]))
        self.assertIn("нет связи", snap["status"])
        self.assertLessEqual(len(self.downloads), 8)  # сеть пропала — дальше картинки не запрашиваются
        # Fang на скриншоте B — та же картинка, что у Fang с прочитанным названием на A: одна стопка.
        self.assertIn("узнано плиток по картинке: 1", snap["status"])
        self.assertTrue(snap["calcStatus"].startswith("Найдено предметов: 5"))
        # Остальные плитки без названия — пользователю: Elderwood Scythe, Luger, Icebreaker x2 (A)
        # и Icebreaker с непрочитанным значком (B).
        self.assertEqual(len(snap["unsure"]), 4, snap["unsure"])
        texts = [row["text"] for row in snap["unsure"]]
        self.assertEqual(sum("выберите сами" in text for text in texts), 3, texts)
        self.assertTrue(all("выберите сами" in text or "не уверен" in text for text in texts), texts)  # похоже — ниже порога
        self.assertEqual(sum("проверьте количество" in row["text"] for row in snap["unsure"]), 1)
        index = next(i for i, row in enumerate(snap["unsure"]) if "×2" in row["text"])
        self.choose_other(index, "Icebreaker")
        self.wait('calc.items.some((item) => item.key === "godlies:Icebreaker")')
        snap = self.snapshot()
        icebreaker = self.rows(snap)["godlies:Icebreaker"]
        self.assertEqual((icebreaker["qty"], icebreaker["via"]), (2, "user"))
        self.assertEqual(len(snap["unsure"]), 2)  # Icebreaker на B узнан по выбранной картинке
        self.assertTrue(snap["calcStatus"].startswith("Найдено предметов: 6"))
        # «не считать» убирает плитку из списка, строки не появляется
        self.js("""
            const select = document.querySelector("#calcPicTiles .pic-tile select");
            select.value = "__skip";
            select.dispatchEvent(new Event("change"));
        """)
        snap = self.snapshot()
        self.assertEqual((len(snap["unsure"]), len(snap["rows"])), (1, 6))
        # Вариант в строке выбирает пользователь: в строке две плитки (ружьё и нож), поэтому
        # их картинки не запоминаются как выбранный предмет.
        other = ({"vintages:Cowboy", "rares:Cowboy"} - {cowboy}).pop()
        self.js("""
            const row = [...$("calcBody").children].find((tr) => tr.querySelector("select.variant"));
            const select = row.querySelector("select.variant");
            select.value = "1";
            select.dispatchEvent(new Event("change"));
        """)
        rows = self.rows(self.snapshot())
        self.assertEqual((rows[other]["qty"], cowboy in rows), (41, False))
        self.assertNotIn("проверьте вариант", rows[other]["badges"])
        self.assertTrue(self.learned("godlies:Icebreaker"))  # выбор для одной плитки — запомнен
        self.assertNotIn(other, json.loads(self.handler.features.snapshot_json())["learned"])

    def qty_of(self, key, value):
        """Пользователь вписал количество в строке предмета."""
        self.js(f"""
            const index = calc.items.findIndex((item) => item.key === {json.dumps(key)});
            const input = [...$("calcBody").children][index].querySelector("input.qty");
            input.value = "{value}";
            input.dispatchEvent(new Event("input"));
            input.dispatchEvent(new Event("change"));
            return true;
        """)

    def test_inventory_read_as_text_is_still_compared_by_picture(self):
        # Названия на плитках не прочитались — сервер прочитал скриншот как текст, и слово «Seerr»
        # нашло Seer. Плитки (по картинке их видно) всё равно сравниваются, а «Seer» не считается.
        png, truth = shot([("ancients:Harvester", "x3"), ("godlies:Fang", "x7"), ("godlies:Icebreaker", "x2")], 21)
        self.inventories[png] = (text_words("Seerr"), ocr_tiles(truth, unread=set(picgen.ITEMS)))
        self.truths[png] = ("C", truth)
        self.run_calc(png)
        snap = self.snapshot()
        self.assertEqual({key: row["qty"] for key, row in self.rows(snap).items()},
                         {"ancients:Harvester": 3, "godlies:Fang": 7, "godlies:Icebreaker": 2}, snap)
        self.assertTrue(all("по картинке" in row["badges"] for row in snap["rows"]), snap["rows"])
        self.assertIn("не учтено прочитанное как текст (Seer)", snap["status"])
        self.assertTrue(snap["calcStatus"].startswith("Найдено предметов: 3"), snap["calcStatus"])

    def test_offline_inventory_read_as_text_keeps_its_rows(self):
        # Без связи и без сохранённых картинок: плитки сравнить не с чем — скриншот, прочитанный как
        # текст, считается по тексту, как без картинок (а не плитками, которые решать пользователю).
        self.offline = True
        png, truth = shot([("ancients:Harvester", "x3"), ("godlies:Fang", "x7")], 22)
        self.inventories[png] = (text_words("Harvester x3", "Fang x7"), ocr_tiles(truth, unread=set(picgen.ITEMS)))
        self.truths[png] = ("E", truth)
        self.run_calc(png)
        snap = self.snapshot()
        self.assertEqual({key: row["qty"] for key, row in self.rows(snap).items()},
                         {"ancients:Harvester": 3, "godlies:Fang": 7}, snap)
        self.assertEqual(snap["unsure"], [])  # не вдобавок к строкам: их не посчитать дважды
        self.assertIn("посчитан по прочитанному тексту", snap["status"])
        self.assertNotIn("не учтено", snap["status"])
        self.assertTrue(snap["calcStatus"].startswith("Найдено предметов: 2"), snap["calcStatus"])

    def test_names_read_as_text_are_offered_for_unsure_tiles(self):
        # Картинки есть, но плитки по ним не узнаны: строки текста остаются, а прочитанное название
        # предлагается у плиток; выбранная плитка не добавляется к строке текста, а уточняет её.
        self.missing.update(url for url in self.pictures if "harvester" in url or "fang" in url or "scythe" in url)
        png, truth = shot([("ancients:Harvester", "x3"), ("godlies:Fang", "x7")], 22)
        self.inventories[png] = (text_words("Fang", "Back"), ocr_tiles(truth, unread=set(picgen.ITEMS)))
        self.truths[png] = ("E", truth)
        self.run_calc(png)
        snap = self.snapshot()
        self.assertEqual({key: row["qty"] for key, row in self.rows(snap).items()}, {"godlies:Fang": 1}, snap)
        self.assertEqual(len(snap["unsure"]), 2, snap)
        for row in snap["unsure"]:  # предлагается в списке у плиток этого скриншота
            self.assertIn("godlies:Fang", row["options"])
        index = next(i for i, row in enumerate(snap["unsure"]) if "×7" in row["text"])
        self.choose_other(index, "Fang")
        self.wait('document.querySelectorAll("#calcPicTiles .pic-tile").length === 1')
        self.assertEqual({key: row["qty"] for key, row in self.rows(self.snapshot()).items()}, {"godlies:Fang": 7})
        self.choose_other(0, "Harvester")
        self.wait('document.querySelectorAll("#calcPicTiles .pic-tile").length === 0')
        snap = self.snapshot()
        self.assertEqual({key: row["qty"] for key, row in self.rows(snap).items()},
                         {"godlies:Fang": 7, "ancients:Harvester": 3}, snap)
        self.assertTrue(snap["calcStatus"].startswith("Найдено предметов: 2"), snap["calcStatus"])

    def test_text_screenshot_without_tiles_keeps_its_text(self):
        canvas = picgen.Canvas(320, 120, (40, 40, 44, 255))
        canvas.rect(10, 10, 200, 24, (230, 230, 230))
        png = canvas.png(alpha=False)
        self.inventories[png] = (text_words("Harvester x2", "Seer"), [])
        self.run_calc(png)
        snap = self.snapshot()
        self.assertEqual({key: row["qty"] for key, row in self.rows(snap).items()},
                         {"ancients:Harvester": 2, "godlies:Seer": 1})
        self.assertEqual((snap["unsure"], snap["status"]), ([], ""))

    def test_clipped_tile_without_name_is_not_lost(self):
        # Нижний ряд обрезан краем скриншота: у Icebreaker виден значок «x2», а полоса с названием — нет.
        cells = [("ancients:Harvester", "x3"), ("godlies:Seer", "x12"), ("godlies:Fang", "x7"),
                 ("chromas:Chroma Seer", None), ("godlies:Icebreaker", "x2")]
        canvas, truth = picgen.grid(cells, width=WIDTH, seed=31)
        png = cut_bottom(canvas, truth[-1]["banner"][0] + 4)
        tiles = ocr_tiles(truth[:4], unread=set(), missed=set())
        clipped = ocr_tiles(truth[4:], unread=set(picgen.ITEMS), missed=set())[0]
        clipped.update(clipped=True, name_box=None)
        self.inventories[png] = ({"width": 0, "height": 0, "words": []}, tiles + [clipped])
        self.truths[png] = ("D", truth)
        self.run_calc(png)
        snap = self.snapshot()
        self.assertEqual({key: row["qty"] for key, row in self.rows(snap).items()},
                         {"ancients:Harvester": 3, "godlies:Seer": 12, "godlies:Fang": 7, "chromas:Chroma Seer": 1})
        self.assertEqual(len(snap["unsure"]), 1, snap)  # не пропала молча: выбирает пользователь
        self.assertTrue(snap["unsure"][0]["thumb"])
        self.assertIn("видна не целиком", snap["unsure"][0]["text"])
        self.assertIn("×2", snap["unsure"][0]["text"])
        self.assertIn("не уверен: 1", snap["status"])
        self.choose_other(0, "Icebreaker")
        self.wait('calc.items.some((item) => item.key === "godlies:Icebreaker")')
        icebreaker = self.rows(self.snapshot())["godlies:Icebreaker"]
        self.assertEqual((icebreaker["qty"], icebreaker["via"]), (2, "user"))
        time.sleep(0.3)
        self.assertNotIn("godlies:Icebreaker", json.loads(self.handler.features.snapshot_json())["learned"])

    def test_choosing_a_variant_that_already_has_a_row_merges_them(self):
        # У ножа Cowboy картинки нет: на A вариант не выбрать, а на B то же ружьё узнано по картинке.
        self.missing.add(MARKET["rares:Cowboy"])
        cells = [("vintages:Cowboy", "x3"), ("godlies:Fang", "x7")]
        png_a, truth_a = shot(cells, 41)
        png_b, truth_b = shot(cells, 42)
        self.inventories[png_a] = ({"width": 0, "height": 0, "words": []}, ocr_tiles(truth_a, unread=set(), missed=set()))
        self.truths.update({png_a: ("A2", truth_a), png_b: ("B2", truth_b)})
        self.run_calc(png_a, png_b)
        snap = self.snapshot()
        self.assertEqual(sorted((row["key"], row["qty"]) for row in snap["rows"]),
                         [("godlies:Fang", 7), ("rares:Cowboy", 3), ("vintages:Cowboy", 3)], snap)
        self.assertIn("проверьте вариант", self.rows(snap)["rares:Cowboy"]["badges"])
        self.js("""
            const row = [...$("calcBody").children].find((tr) => tr.querySelector("select.variant"));
            const select = row.querySelector("select.variant");
            select.value = "1";  // ружьё (второй вариант строки ножа)
            select.dispatchEvent(new Event("change"));
            return true;
        """)
        snap = self.snapshot()
        self.assertEqual(sorted((row["key"], row["qty"]) for row in snap["rows"]),
                         [("godlies:Fang", 7), ("vintages:Cowboy", 3)], snap)  # одна стопка — одна строка
        self.assertTrue(snap["calcStatus"].startswith("Найдено предметов: 2"), snap["calcStatus"])

    def test_typed_quantity_is_kept(self):
        self.offline = True
        cells = [("godlies:Seer", "x12"), ("godlies:Fang", "x7")]
        png_a, truth_a = shot(cells, 51)
        png_b, truth_b = shot(cells, 52)
        tiles = ocr_tiles(truth_a, unread=set(), missed=set())
        tiles[0].update(score=0.9)  # «Seer» прочитано приблизительно — его картинка не запоминается
        self.inventories[png_a] = ({"width": 0, "height": 0, "words": []}, tiles)
        self.truths.update({png_a: ("A3", truth_a), png_b: ("B3", truth_b)})
        self.run_calc(png_a, png_b)
        snap = self.snapshot()
        self.assertEqual({key: row["qty"] for key, row in self.rows(snap).items()}, {"godlies:Seer": 12, "godlies:Fang": 7})
        self.assertEqual(len(snap["unsure"]), 1, snap)  # Seer со скриншота B
        self.qty_of("godlies:Seer", 5)  # «продаю только 5»
        self.choose_other(0, "Seer")
        self.wait('document.querySelectorAll("#calcPicTiles .pic-tile").length === 0')
        self.assertEqual(self.rows(self.snapshot())["godlies:Seer"]["qty"], 5)

    def test_prices_are_asked_in_parts(self):
        self.watch_posts()
        keys = [markets.item_id(item) for item in stock()] + [f"godlies:Fake {n}" for n in range(1200)]
        self.js(f"""
            PicTiles._state.pics.templates = new Map();
            await PicTiles._state.fetchTemplates({json.dumps(keys)});
            return true;
        """)
        sizes = self.js('return seenPosts.filter(([url]) => url === "/api/calc").map(([url, body]) => body.picked.length);')
        self.assertEqual(sum(sizes), len(keys))
        self.assertTrue(all(size <= calc_module.MAX_PICKED for size in sizes), sizes)
        known = self.js("return [...PicTiles._state.pics.templates.keys()];")
        self.assertEqual(sorted(known), sorted(markets.item_id(item) for item in stock()))

    def test_broken_pictures_are_not_a_lost_connection(self):
        site = [item["image"] for item in stock() if item.get("image")]
        self.corrupt.update(site[::2])  # больше OFFLINE_AFTER плохих картинок: испорченные
        self.busy.update(site[1::2])    # и «сайт занят»
        self.run_calc(self.png_a, self.png_b)
        snap = self.snapshot()
        state = self.js("return { down: [...PicTiles._state.refs.down], failed: [...PicTiles._state.refs.failed] };")
        self.assertEqual(state["down"], [])
        self.assertNotIn("нет связи", snap["status"])
        self.assertIn("godlies:Seer|site", state["failed"])
        self.assertEqual(set(site) - {url for url in site if url in self.downloads}, set())  # спрошены все
        self.assertEqual(self.rows(snap)["godlies:Seer"]["qty"], 12)

    def test_blocking_site_is_not_asked_again_and_again(self):
        self.site_blocked = True
        self.run_calc(self.png_a, self.png_b)
        snap = self.snapshot()
        site = [url for url in self.downloads if url.startswith(SITE)]
        self.assertTrue(1 <= len(site) <= imgproxy.PARALLEL, site)  # дальше сервер отвечает сам: пауза сайта
        self.assertIn(MARKET["rares:Cowboy"], self.downloads)  # другой сайт картинок — как обычно
        self.assertIn("не пускает", snap["status"])
        self.assertEqual(self.rows(snap)["ancients:Harvester"]["qty"], 3)  # названия — как раньше

    def market_everywhere(self, but=()):
        """Картинки StarPets у всех предметов (как у настоящих: почти всё продаётся на StarPets), кроме but."""
        market = {key: f"https://cdn.starpets.gg/MM/110x110/{number}.webp"
                  for number, key in enumerate(picgen.ITEMS) if key not in NO_PICTURE and key not in but}
        self.pictures = dict(self.pictures)
        for key, url in market.items():
            self.pictures[url] = picgen.reference(key).png()
        self.handler.monitor = FakeMonitor(market)
        return market

    def test_adding_screenshots_downloads_nothing(self):
        # Картинки предметов — только в расчёте и только нужные, а не все сразу при добавлении скриншота.
        self.add_files(self.png_a, self.png_b)
        time.sleep(2)
        self.assertEqual(self.downloads, [])

    def test_bulk_pictures_come_from_starpets(self):
        # Для плиток без названия нужны картинки всех предметов: их раздаёт CDN StarPets. С сайта
        # предметов (под защитой от ботов) — только картинки вариантов одного названия.
        market = self.market_everywhere()
        self.run_calc(self.png_a, self.png_b)
        rows = self.rows(self.snapshot())
        self.assertEqual((rows["godlies:Elderwood Scythe"]["qty"], rows["vintages:Cowboy"]["qty"]), (1, 40))
        self.assertEqual([url for url in self.downloads if url.startswith(SITE)],
                         [SITE + "cowboy-vintages.webp"])  # нож Cowboy — только на StarPets
        self.assertEqual(set(market.values()) - set(self.downloads), set())

    def test_unreachable_picture_site_does_not_stop_the_other(self):
        # supremevalues.com не открывается (провайдер), StarPets — открывается. Картинки предметов,
        # которые есть только на сайте, не загрузятся, но картинки StarPets — да, в том же расчёте.
        only_site = {"ancients:Harvester", "godlies:Seer", "chromas:Chroma Seer", "godlies:Fang", "godlies:Icebreaker"}
        market = self.market_everywhere(but=only_site)
        self.site_down = True
        self.run_calc(self.png_a, self.png_b)
        snap = self.snapshot()
        rows = self.rows(snap)
        self.assertEqual({key: row["qty"] for key, row in rows.items()}, {
            "ancients:Harvester": 3, "godlies:Seer": 12, "chromas:Chroma Seer": 1, "godlies:Fang": 7,
            "godlies:Elderwood Scythe": 1, "vintages:Cowboy": 40, "rares:Cowboy": 1}, snap)
        for key in ("godlies:Elderwood Scythe", "vintages:Cowboy", "rares:Cowboy"):  # по картинкам StarPets
            self.assertIn("по картинке", rows[key]["badges"], key)
        self.assertIn("нет связи с supremevalues.com", snap["status"])
        self.assertNotIn("картинки предметов не загрузились", snap["status"])
        self.assertEqual(self.js("return [...PicTiles._state.refs.down];"), ["supremevalues.com"])
        feats = json.loads(self.handler.features.snapshot_json())["feats"]
        self.assertEqual({key for key, entry in feats.items() if "market" in entry}, set(market))
        self.assertLessEqual(len([url for url in self.downloads if url.startswith(SITE)]), imgproxy.OFFLINE_AFTER + 2)

    def test_unread_badges_ask_to_check_quantity(self):
        # Значки скриншота без плиток распознавания не прочитались (ошибка распознавания): количество
        # его плиток не угадывается молча — «проверьте количество», и у плитки, где браузер значка
        # не увидел (мелкое «x2» он может пропустить).
        png, truth = shot([("godlies:Fang", "x7"), ("godlies:Seer", None)], 71)
        self.truths[png] = ("F", truth)
        self.badge_errors.add("F")
        self.run_calc(png)
        snap = self.snapshot()
        rows = self.rows(snap)
        self.assertEqual(set(rows), {"godlies:Fang", "godlies:Seer"}, snap)
        for key in rows:
            self.assertIn("проверьте количество", rows[key]["badges"], key)
        self.assertIn("количество не прочитано", snap["status"])

    def cowboy_shot(self):
        """Скриншот: Cowboy (ружьё x3, серая полоса — вариант не ясен) и Fang x7 — названия прочитаны."""
        png, truth = shot([("vintages:Cowboy", "x3"), ("godlies:Fang", "x7")], 61)
        self.inventories[png] = ({"width": 0, "height": 0, "words": []}, ocr_tiles(truth, unread=set(), missed=set()))
        self.truths[png] = ("C2", truth)
        self.gate = threading.Event()  # картинки придут, когда пользователь уже что-то сделал
        self.addCleanup(self.gate.set)
        self.run_calc(png, wait=False)
        return self.rows(self.snapshot())

    def test_removed_row_does_not_come_back(self):
        rows = self.cowboy_shot()
        cowboy = next(key for key in rows if key.endswith(":Cowboy"))
        self.assertIn("проверьте вариант", rows[cowboy]["badges"])
        self.js(f"""
            const index = calc.items.findIndex((item) => item.key === {json.dumps(cowboy)});
            [...$("calcBody").children][index].querySelector("button.icon").click();
            return true;
        """)
        self.gate.set()
        self.js("await PicTiles.idle(); return true;")
        snap = self.snapshot()
        self.assertEqual([row["key"] for row in snap["rows"]], ["godlies:Fang"], snap)  # ни ружьё, ни нож
        self.assertNotIn("вариант выбран по картинке", snap["status"])
        self.assertTrue(snap["calcStatus"].startswith("Найдено предметов: 1"), snap["calcStatus"])

    def test_variant_chosen_by_user_is_not_relabelled(self):
        rows = self.cowboy_shot()
        server = next(key for key in rows if key.endswith(":Cowboy"))
        chosen = ({"vintages:Cowboy", "rares:Cowboy"} - {server}).pop()
        self.js("""
            const select = document.querySelector("#calcBody select.variant");
            select.value = "1";
            select.dispatchEvent(new Event("change"));
            return true;
        """)
        self.gate.set()
        self.js("await PicTiles.idle(); return true;")
        snap = self.snapshot()
        rows = self.rows(snap)
        self.assertEqual(sorted(rows), sorted(["godlies:Fang", chosen]), snap)  # выбор пользователя остался
        self.assertNotIn("по картинке", rows[chosen]["badges"])
        self.assertNotIn("вариант выбран по картинке", snap["status"])


if __name__ == "__main__":
    unittest.main()
