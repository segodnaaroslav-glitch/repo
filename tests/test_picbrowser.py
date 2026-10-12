"""Калькулятор со скриншотами в настоящем браузере (скрытый Chromium/Edge): программа целиком,
распознавание текста и площадки — подделки, картинки предметов — нарисованные tests/picgen.py."""

import base64
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from supreme import cdp, fetcher, imgproxy, markets, ocr, parser, server, store
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


def ocr_tiles(truth):
    """Плитки, как их вернул бы tileocr.recognize (место приблизительное)."""
    tiles = []
    for place in truth:
        key = place["key"]
        if key in OCR_MISSED:
            continue
        name = None if key in UNREAD_NAMES else picgen.ITEMS[key][0]
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


@unittest.skipUnless(BROWSER, "нет Edge/Chrome/Chromium")
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
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        data = store.empty_data()
        items = stock()
        for category in sorted({i["category"] for i in items}):
            store.replace_category(data, category, [i for i in items if i["category"] == category], "site")
        store.save(data, self.folder / "values.json")
        self.httpd = server.make_server(port=0, data_path=self.folder / "values.json", update=lambda log: None,
                                        monitor=FakeMonitor(MARKET))
        self.handler = self.httpd.RequestHandlerClass
        self.downloads, self.offline = [], False
        self.handler.images = imgproxy.ImageCache(self.folder / "images", download=self.download)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        for patch in (mock.patch.object(ocr, "read_inventory", side_effect=self.read_inventory),
                      mock.patch.object(ocr, "recognize_regions", side_effect=self.recognize_regions)):
            patch.start()
            self.addCleanup(patch.stop)
        self.badge_calls = []
        self.browser.open(f"http://127.0.0.1:{self.httpd.server_address[1]}/", settle=0.2)
        self.wait("state.data.items.length > 0 && state.dataVersion !== null")

    # --- подделки ---

    def download(self, url):
        self.downloads.append(url)
        if self.offline:
            raise imgproxy.ImageError(503, "нет связи", "network")
        if url not in self.pictures:
            raise imgproxy.ImageError(404, "нет картинки")
        return self.pictures[url], ("image/png", ".png")

    def read_inventory(self, raw, matcher):
        size = {"width": 0, "height": 0, "words": []}
        if raw == self.png_a:
            return size, ocr_tiles(self.truth_a)
        return size, []  # B: плиток не нашлось

    def recognize_regions(self, raw, regions, **kwargs):
        name, truth = ("A", self.truth_a) if raw == self.png_a else ("B", self.truth_b)
        self.badge_calls.append((name, len(regions)))
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

    def run_calc(self, *pngs):
        shots = json.dumps([base64.b64encode(png).decode() for png in pngs])
        return self.js(f"""
            $("calcClear").click();
            const files = {shots}.map((b64, i) => new File([Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))],
                                                            `shot${{i}}.png`, {{ type: "image/png" }}));
            addCalcFiles(files);
            while (calc.images.some((image) => !image.data)) await new Promise((r) => setTimeout(r, 20));
            await runCalc();
            await PicTiles.idle();
            return true;
        """)

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

    def test_pictures_decide_variants_and_nameless_tiles(self):
        self.run_calc(self.png_a, self.png_b)
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
        self.wait("!PicTiles._state.refs.warming")  # остальные картинки посчитаны в фоне
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

if __name__ == "__main__":
    unittest.main()
