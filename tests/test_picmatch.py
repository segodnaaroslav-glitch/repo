"""Сравнение плиток инвентаря по картинке (web/picmatch.js) — в Node на синтетических скриншотах
(tests/picgen.py) и на настоящей плитке из игры."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import picgen

HERE = Path(__file__).parent
NODE = shutil.which("node")
SURE, MARGIN = 0.80, 0.08  # как PicMatch.PIC_SURE / PIC_MARGIN

# Сетка 4 x 3: «трудные» пары — одинаковый силуэт с другими цветами (Seer и Chroma Seer,
# Harvester и Elderwood Scythe) и одинаковое название (Cowboy — ружьё и нож).
CELLS = [("ancients:Harvester", "x3"), ("godlies:Elderwood Scythe", None), ("godlies:Seer", "x12"),
         ("chromas:Chroma Seer", None), ("godlies:Icebreaker", "x2"), ("vintages:Cowboy", "x40"),
         ("rares:Cowboy", None), None, ("godlies:Fang", "x7"), ("commons:Luger", None)]


def run_node(job, folder):
    path = Path(folder) / "job.json"
    path.write_text(json.dumps(job), encoding="utf-8")
    result = subprocess.run([NODE, str(HERE / "js" / "picmatch_check.js"), str(path)],
                            capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise AssertionError(result.stderr[-2000:])
    return json.loads(result.stdout)


def write(folder, name, canvas):
    path = Path(folder) / (name + ".rgba")
    path.write_bytes(canvas.rgba())
    return str(path)


def reference_files(folder, keys=None, decoys=0):
    refs = {key: [write(folder, "ref-" + key.replace(":", "-").replace(" ", "_"), picgen.reference(key))]
            for key in keys or picgen.ITEMS}
    for number in range(decoys):
        refs[f"misc:Decoy {number}"] = [write(folder, f"decoy-{number}", picgen.decoy(number))]
    return refs


def find(truth, tile, slack=2):
    """Плитка из ответа picmatch -> её место на синтетическом скриншоте (или None)."""
    return next((t for t in truth if abs(t["x0"] - tile["x0"]) <= slack and abs(t["x1"] - tile["x1"]) <= slack
                 and abs(t["icon_top"] - tile["iconTop"]) <= slack + 1), None)


@unittest.skipUnless(NODE, "нет Node.js")
class PicMatchNodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        folder = cls.tmp.name
        cls.truth, images = {}, []
        for width in (130, 90, 60, 45):
            canvas, cls.truth[str(width)] = picgen.grid(CELLS, width=width, seed=width)
            boxes = [{"box": [t["x0"] - 1 + (i % 3) - 1, t["icon_top"] - 2 + (i % 2), width, round(1.31 * width)]}
                     for i, t in enumerate(cls.truth[str(width)])]  # место плитки, как его оценивает распознавание текста
            images.append({"name": str(width), "file": write(folder, f"grid{width}", canvas), "boxes": boxes})
        # шум сжатия и предмет без картинки-эталона (Fang)
        canvas, cls.truth["noisy"] = picgen.grid(CELLS, width=100, seed=5, cut_left=False)
        picgen.add_noise(canvas, 6)
        images.append({"name": "noisy", "file": write(folder, "noisy", canvas)})
        _, _, data = picgen.png_decode((HERE / "fixtures" / "mm2_cowboy_x40.png").read_bytes())
        real = picgen.Canvas(163, 177)
        real.px = data
        images.append({"name": "real", "file": write(folder, "real", real)})
        refs = reference_files(folder, decoys=60)
        cls.refs_no_fang = {k: v for k, v in refs.items() if k != "godlies:Fang"}
        cls.result = run_node({"images": images[:4], "refs": refs}, folder)
        cls.hard = run_node({"images": images[4:], "refs": cls.refs_no_fang}, folder)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def images(self):
        return {image["name"]: image for image in self.result["images"] + self.hard["images"]}

    def test_every_tile_found_with_its_badge(self):
        for name, image in self.images().items():
            if name == "real":
                continue
            truth = self.truth[name]
            full = [t for t in image["tiles"] if not t["partial"]]
            self.assertEqual(len(full), len(truth), name)  # обрезанные у края и пустые места — не плитки
            for tile in full:
                place = find(truth, tile)
                self.assertIsNotNone(place, (name, tile))
                self.assertEqual(bool(tile["badge"]), bool(place["badge"]), (name, place["key"]))
                self.assertLessEqual(abs(tile["banner"]["y0"] - place["banner"][0]), 1)

    def test_banner_colour_buckets(self):
        buckets = {}
        for name in ("130", "90", "noisy"):
            image = self.images()[name]
            for tile in image["tiles"]:
                buckets.setdefault(find(self.truth[name], tile)["key"].split(":")[0], set()).add(tile["bucket"])
        self.assertEqual(buckets["commons"], {"grey"})
        self.assertEqual(buckets["vintages"], {"50"})
        self.assertEqual(buckets["rares"], {"210"})
        vintage = next(t for t in self.images()["130"]["tiles"] if t["bucket"] == "50")
        self.assertEqual(vintage["prior"], ["vintages"])

    def test_picture_recognised_and_never_surely_wrong(self):
        for name in ("130", "90", "60", "45"):
            image = self.images()[name]
            for tile in image["tiles"]:
                key = find(self.truth[name], tile)["key"]
                self.assertEqual(tile["top"][0][0], key, (name, tile["top"]))
                self.assertIn(tile["sure"], (key, None), (name, tile["top"]))
                wrong = max(score for other, score in tile["top"] if other != key)
                self.assertLess(wrong, SURE - MARGIN, (name, key, tile["top"]))
                if int(name) >= 90:  # крупные плитки — уверенно
                    self.assertEqual(tile["sure"], key, (name, tile["top"]))
            sure = sum(1 for tile in image["tiles"] if tile["sure"])
            self.assertGreaterEqual(sure, len(image["tiles"]) // 2, name)

    def test_same_name_and_recoloured_pairs_told_apart(self):
        for name in ("130", "90"):
            scores = {find(self.truth[name], t)["key"]: dict(t["top"]) for t in self.images()[name]["tiles"]}
            for a, b in (("vintages:Cowboy", "rares:Cowboy"), ("godlies:Seer", "chromas:Chroma Seer"),
                         ("ancients:Harvester", "godlies:Elderwood Scythe")):
                for own, other in ((a, b), (b, a)):
                    self.assertGreaterEqual(scores[own][own] - scores[own].get(other, 0), 0.3, (name, own, other))

    def test_approximate_place_from_text_recognition(self):
        for name in ("130", "90", "60"):
            image = self.images()[name]
            for place, found in zip(self.truth[name], image["boxes"]):
                self.assertIsNotNone(found, name)
                self.assertEqual(found["top"][0][0], place["key"], (name, found["top"]))
                self.assertIn(found["sure"], (place["key"], None))
                if name == "130":
                    self.assertEqual(found["sure"], place["key"], found["top"])

    def test_noise_and_item_without_reference(self):
        image = self.images()["noisy"]
        for tile in image["tiles"]:
            key = find(self.truth["noisy"], tile)["key"]
            if key == "godlies:Fang":  # эталона нет — не угадывается, решает пользователь
                self.assertIsNone(tile["sure"], tile["top"])
                self.assertLess(tile["top"][0][1], SURE, tile["top"])
            else:
                self.assertIn(tile["sure"], (key, None), tile["top"])
                self.assertEqual(tile["top"][0][0], key, tile["top"])

    def test_real_tile_from_the_game(self):
        tiles = self.images()["real"]["tiles"]
        full = [t for t in tiles if not t["partial"]]
        self.assertEqual(len(full), 1, tiles)
        tile = full[0]
        self.assertEqual((tile["x0"], tile["x1"], tile["iconTop"], tile["iconBottom"]), (18, 147, 3, 138))
        self.assertEqual((tile["banner"]["y0"], tile["banner"]["y1"]), (142, 172))
        self.assertIsNotNone(tile["badge"])  # «x40»
        self.assertGreaterEqual(tile["badge"]["x0"], 100)
        self.assertEqual(tile["bucket"], "50")  # жёлтая полоса — Vintage
        self.assertIsNone(tile["sure"])  # эталона этого револьвера нет — не угадывается

    def test_features_survive_packing(self):
        for key, size, score in self.result["roundtrip"]:
            self.assertLess(size, 4096)  # imgproxy.MAX_FEAT_CHARS
            self.assertGreater(score, 0.97, key)

    def test_decision_helpers(self):
        result = run_node({
            "sure": [[{"key": "a", "score": 0.85}, {"key": "b", "score": 0.79}],
                     [{"key": "a", "score": 0.85}, {"key": "b", "score": 0.76}],
                     [{"key": "a", "score": 0.79}],
                     [{"key": "a", "score": 0.95}, {"key": "a", "score": 0.94}, {"key": "b", "score": 0.5}],
                     []],
            # полоса (200, 60, 180) — корзина «300»: выученное по уверенным плиткам важнее таблицы,
            # но только когда одна категория явно преобладает
            "prior": [[[230, 200, 0], {}], [[200, 60, 180], {"300": {"chromas": 4}}],
                      [[200, 60, 180], {"300": {"godlies": 3, "chromas": 2}}], [[200, 200, 200], {}], [[128, 128, 128], {}]],
            "apply": [[[{"key": "godlies:A", "score": 0.80}, {"key": "chromas:B", "score": 0.82}], ["godlies"]]],
            "merge": [{"id": "0:0", "shot": 0, "key": "x", "qty": 3}, {"id": "1:0", "shot": 1, "key": "x", "qty": 5},
                      {"id": "1:1", "shot": 1, "key": "y", "qty": 1}, {"id": "1:2", "shot": 1, "key": "y", "qty": 1},
                      {"id": "0:1", "shot": 0, "key": None, "qty": 9}],
        }, self.tmp.name)
        self.assertEqual(result["sure"], [None, "a", None, "a", None])
        self.assertEqual(result["prior"], [["vintages"], ["chromas"], ["godlies"], ["commons"], None])
        self.assertEqual(result["apply"], [[["godlies:A", 0.8], ["chromas:B", 0.7544]]])  # подсказка мягкая
        # один предмет на двух скриншотах — одна стопка (больше из двух); на одном — две плитки
        self.assertEqual(result["merge"], [["x", ["1:0"]], ["y", ["1:1", "1:2"]]])


if __name__ == "__main__":
    unittest.main()
