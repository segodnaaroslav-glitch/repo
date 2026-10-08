import json
import time
import unittest
from pathlib import Path

from supreme import tileocr

NAMES = json.loads((Path(__file__).parent / "fixtures" / "mm2_names.json").read_text(encoding="utf-8"))

W, PITCH_X, PITCH_Y, X0, Y0 = 130, 139, 185, 20, 30


def name_words(text, left, top, h=21, pass_="white", line="l", cut=0, bg=(230, 200, 0)):
    """Word boxes of a banner name, auto-shrunk like Roblox TextScaled (max 0.9 W wide)."""
    parts = text.split(" ")
    chars = sum(len(p) for p in parts)
    h = min(h, 0.9 * W / (0.62 * chars + 0.3 * (len(parts) - 1)))
    total = 0.62 * h * chars + 0.3 * h * (len(parts) - 1)
    x = left + W / 2.0 - total / 2.0
    cy = top + 1.21 * W
    out = []
    for p in parts:
        w = 0.62 * h * len(p)
        out.append({"text": p, "x": int(round(x)), "y": int(round(cy - h / 2)), "w": int(round(w)),
                    "h": int(round(h)), "pass": pass_, "line": line, "cut": cut, "bg": list(bg)})
        x += w + 0.3 * h
    return out


def badge_words(text, left, top, pass_="white", line="b", cut=0):
    """'x40' at the top-right of the tile; 'x 40' gives two words."""
    h = 16
    right = left + W - 6.5
    cy = top + 0.15 * W
    parts = text.split(" ")
    widths = [0.55 * h * len(p) for p in parts]
    x = right - sum(widths) - 0.35 * h * (len(parts) - 1)
    out = []
    for p, w in zip(parts, widths):
        out.append({"text": p, "x": int(round(x)), "y": int(round(cy - h / 2)), "w": int(round(w)), "h": h,
                    "pass": pass_, "line": line, "cut": cut, "bg": [58, 58, 58]})
        x += w + 0.35 * h
    return out


def grid(tiles, pass_="white", noise=True):
    """tiles: {(row, col): (name_text or None, badge_text or None)} -> words of one pass."""
    words = []
    for (r, c), (name, badge) in tiles.items():
        left, top = X0 + c * PITCH_X, Y0 + r * PITCH_Y
        if name:
            words += name_words(name, left, top, pass_=pass_, line="%s:0:%d" % (pass_, 2 * r + 1))
        if badge:
            words += badge_words(badge, left, top, pass_=pass_, line="%s:0:%d" % (pass_, 2 * r))
        if noise:  # specks from the weapon picture
            words.append({"text": "'", "x": left + 40, "y": top + 60, "w": 4, "h": 5, "pass": pass_,
                          "line": "%s:0:n%d%d" % (pass_, r, c), "cut": 0, "bg": [60, 60, 60]})
            words.append({"text": "lI", "x": left + 30, "y": top + 80, "w": 9, "h": 14, "pass": pass_,
                          "line": "%s:0:m%d%d" % (pass_, r, c), "cut": 0, "bg": [55, 55, 55]})
    return words


TRUTH = {
    (0, 0): ("Cowboy", "x40"), (0, 1): ("Seer", None), (0, 2): ("Icebreaker", None), (0, 3): ("Harvester", "x2"),
    (1, 0): ("Elderwood Scythe", "x3"), (1, 1): ("Chroma Darkbringer", None), (1, 2): ("Xmas", "x12"),
    (1, 3): ("Sparkle10", "x 5"),
}
EXPECTED = [("Cowboy", 40), ("Seer", 1), ("Icebreaker", 1), ("Harvester", 2),
            ("Elderwood Scythe", 3), ("Chroma Darkbringer", 1), ("Xmas", 12), ("Sparkle10", 5)]


def result_pairs(tiles):
    return [(t["name"], t["qty"]) for t in tiles]


class BadgeTests(unittest.TestCase):
    def test_variants(self):
        for text, want in [("x40", 40), ("X40", 40), ("×40", 40), ("x4O", 40), ("x 40", 40), ("xl0", 10),
                           ("(x12)", 12), ("x2.", 2), ("х40", 40), ("xIO", 10), ("x1000", 1000), ("*3", 3)]:
            self.assertEqual(tileocr.parse_badge(text), want, text)

    def test_not_badges(self):
        for text in ["Xmas", "Xbox", "Xeno", "Xenoshot", "x", "40", "Sparkle10", "xi", "x0", "Phoenix", "x12345"]:
            self.assertIsNone(tileocr.parse_badge(text), text)


class MatcherTests(unittest.TestCase):
    m = tileocr.NameMatcher(NAMES)

    def test_exact_and_typos(self):
        self.assertEqual(self.m.match("Cowboy")[0], "Cowboy")
        self.assertEqual(self.m.match("Cowbov")[0], "Cowboy")
        self.assertEqual(self.m.match("C0wboy")[0], "Cowboy")
        self.assertEqual(self.m.match("Соwbоу")[0], "Cowboy")   # Cyrillic look-alikes
        self.assertEqual(self.m.match("Harvestor")[0], "Harvester")
        self.assertEqual(self.m.match("Elderwood Scyth")[0], "Elderwood Scythe")
        self.assertEqual(self.m.match("Darkbringer")[0], "Darkbringer")

    def test_garbage(self):
        for text in ["p ytter", "xli", "dow", "lI", "Back", "All", "Search", "Toys", "New"]:
            self.assertIsNone(self.m.match(text)[0], text)

    def test_partial_unique(self):
        name, score, partial = self.m.match("Gingersco")
        self.assertEqual(name, "Gingerscope")


class GroupingTests(unittest.TestCase):
    def test_split_line_at_tile_gap(self):
        words = name_words("Cowboy", X0, Y0, line="L") + name_words("Ice breaker", X0 + PITCH_X, Y0, line="L")
        runs = tileocr.split_line(words)
        self.assertEqual([[w["text"] for w in r] for r in runs], [["Cowboy"], ["Ice", "breaker"]])

    def test_long_names_side_by_side(self):
        words = (name_words("Elderwood Scythe", X0, Y0, line="L")
                 + name_words("Chroma Darkbringer", X0 + PITCH_X, Y0, line="L"))
        runs = tileocr.split_line(words)
        self.assertEqual([" ".join(w["text"] for w in r) for r in runs], ["Elderwood Scythe", "Chroma Darkbringer"])

    def test_x_and_number_as_two_words(self):
        names, badges = tileocr.phrases_from_words(badge_words("x 25", X0, Y0, line="B"))
        self.assertEqual([b["qty"] for b in badges], [25])
        self.assertEqual(names, [])


class PipelineTests(unittest.TestCase):
    def run_pipeline(self, words):
        return tileocr.recognize({"words": words}, NAMES)

    def test_clean_single_pass(self):
        self.assertEqual(result_pairs(self.run_pipeline(grid(TRUTH))), EXPECTED)

    def test_example_crop(self):
        # boxes measured on the user's crop (163x177): 'x40' and 'Cowboy'
        words = [{"text": "x40", "x": 109, "y": 15, "w": 33, "h": 16, "pass": "white", "line": "a", "cut": 0, "bg": [58] * 3},
                 {"text": "Cowboy", "x": 45, "y": 150, "w": 77, "h": 21, "pass": "white", "line": "b", "cut": 0,
                  "bg": [230, 200, 0]}]
        tiles = self.run_pipeline(words)
        self.assertEqual(result_pairs(tiles), [("Cowboy", 40)])
        x, y, w, h = tiles[0]["box"]
        self.assertTrue(abs(x - 18) <= 4 and abs(w - 130) <= 6 and abs(y - 3) <= 8, tiles[0]["box"])

    def test_noisy_multi_pass(self):
        white = grid(TRUTH, "white")
        bad = dict(TRUTH)
        bad[(0, 0)] = ("Cowbov", "x4O")
        bad[(0, 3)] = ("Harvestor", "x2")
        bad[(1, 0)] = ("Elderwood Scyth", "X3")
        gray = grid(bad, "grayinv")
        worse = dict(TRUTH)
        worse[(0, 0)] = ("p ytter", None)       # garbage like the user's report
        worse[(0, 1)] = (None, None)            # word lost
        worse[(1, 2)] = ("Xmas", "×12")
        color = grid(worse, "color")
        # the 'white' pass reads a wrong digit for one badge; two other passes outvote it
        for w in white:
            if w["text"] == "x2":
                w["text"] = "x7"
        tiles = self.run_pipeline(white + gray + color)
        self.assertEqual(result_pairs(tiles), EXPECTED)

    def test_overlapping_crops_are_not_double_counted(self):
        words = grid(TRUTH, "white")
        dup = []
        for w in grid(TRUTH, "white", noise=False):
            w = dict(w)
            w["x"] += 1
            w["line"] = w["line"].replace(":0:", ":1:")
            w["cut"] = 1 if w["text"] == "Cowboy" else 0
            dup.append(w)
        self.assertEqual(result_pairs(self.run_pipeline(words + dup)), EXPECTED)

    def test_badge_without_readable_name(self):
        truth = dict(TRUTH)
        truth[(0, 2)] = ("qwzx", "x7")   # name unreadable in both passes, badge fine
        tiles = self.run_pipeline(grid(truth, "white") + grid(truth, "grayinv"))
        got = result_pairs(tiles)
        self.assertIn((None, 7), got)
        self.assertEqual([p for p in got if p[0]], [p for p in EXPECTED if p[0] != "Icebreaker"])

    def test_one_row_no_badges(self):
        truth = {(0, 0): ("Seer", None), (0, 1): ("Icebreaker", None)}
        self.assertEqual(result_pairs(self.run_pipeline(grid(truth))), [("Seer", 1), ("Icebreaker", 1)])

    def test_glued_names_split_by_matcher(self):
        # OCR put two names into one word run (gap rule failed): 'Seer Harvester'
        w = name_words("Seer Harvester", X0, Y0, line="L")
        tiles = self.run_pipeline(w)
        self.assertEqual(sorted(t["name"] for t in tiles), ["Harvester", "Seer"])

    def test_russian_engine(self):
        words = [{"text": "х40", "x": 109, "y": 15, "w": 33, "h": 16, "pass": "white", "line": "a", "cut": 0, "bg": [58] * 3},
                 {"text": "Соwbоу", "x": 45, "y": 150, "w": 77, "h": 21, "pass": "white", "line": "b",
                  "cut": 0, "bg": [230, 200, 0]}]
        self.assertEqual(result_pairs(self.run_pipeline(words)), [("Cowboy", 40)])

    def test_ui_labels_are_not_items(self):
        ui = []
        for i, text in enumerate(["Back", "Search", "All", "Knife", "Inventory", "Weapons"]):
            ui.append({"text": text, "x": 20 + 90 * i, "y": 5, "w": 60, "h": 18, "pass": "white", "line": "ui",
                       "cut": 0, "bg": [28, 32, 44]})
        tiles = self.run_pipeline(ui + grid(TRUTH))
        self.assertEqual(result_pairs(tiles), EXPECTED)

    def test_tiles_cut_by_screenshot_border(self):
        truth = {(0, 0): ("Cowboy", "x40"), (0, 1): ("Seer", None), (0, 2): ("Chroma Seer", "x2")}
        words = grid(truth, noise=False)
        img_w = X0 + 2 * PITCH_X + 70      # the third tile is cut in the middle of its name
        clipped = []
        for w in words:
            if w["x"] >= img_w:
                continue
            if w["x"] + w["w"] > img_w:
                w = dict(w, w=img_w - w["x"], text=w["text"][:3])
            clipped.append(w)
        # the visible half of 'Chroma Seer' is 'Chroma' + 'See'; pretend OCR read just 'Seer'
        for w in clipped:
            if w["text"] == "Chroma":
                w["text"] = "Seer"
        tiles = tileocr.recognize({"words": clipped, "width": img_w, "height": 400}, NAMES)
        self.assertEqual(result_pairs(tiles), [("Cowboy", 40), ("Seer", 1)])

    def test_partial_word_inside_image_is_not_trusted(self):
        truth = {(0, 0): ("Cowboy", "x40"), (0, 2): ("Chroma Seer", None)}
        words = [w for w in grid(truth, noise=False) if w["text"] != "Seer"]   # 'Seer' is beyond the border
        img_w = max(w["x"] + w["w"] for w in words) + 6
        tiles = tileocr.recognize({"words": words, "width": img_w, "height": 400}, NAMES)
        self.assertEqual(result_pairs(tiles), [("Cowboy", 40)])

    def test_badge_area_above_screenshot(self):
        words = grid({(0, 0): ("Seer", None), (0, 1): ("Cowboy", "x40")}, noise=False)
        shift = 60   # screenshot starts below the badges
        cut = [dict(w, y=w["y"] - Y0 - shift) for w in words if w["y"] - Y0 - shift >= 0]
        tiles = tileocr.recognize({"words": cut, "width": 600, "height": 400}, NAMES)
        self.assertEqual(result_pairs(tiles), [("Seer", 1), ("Cowboy", 1)])
        self.assertTrue(all(t["qty_uncertain"] for t in tiles))

    def test_lines_for_calculator(self):
        tiles = self.run_pipeline(grid(TRUTH))
        self.assertEqual(tileocr.tiles_to_lines(tiles)[:2], ["Cowboy x40", "Seer"])

    def test_speed(self):
        big = {}
        for r in range(5):
            for c in range(8):
                big[(r, c)] = (NAMES[(r * 8 + c) * 7 % len(NAMES)], "x%d" % (r + c + 2))
        words = []
        for p in ("white", "grayinv", "whitehi", "color"):
            words += grid(big, p)
        t0 = time.time()
        tiles = self.run_pipeline(words)
        took = time.time() - t0
        self.assertLess(took, 5.0)
        want = [(big[(r, c)][0], r + c + 2) for r in range(5) for c in range(8)]
        self.assertEqual(result_pairs(tiles), want)
        print("\n40 tiles x 4 passes: %.2fs, %d tiles" % (took, len(tiles)))


class OutputParsingTests(unittest.TestCase):
    def test_marker_after_warnings(self):
        out = ("WARNING: something\r\n" + tileocr.MARKER +
               '{"ok":true,"width":163,"height":177,"words":[{"text":"\\u0445\\u00d740","x":1,"y":2,"w":3,"h":4,'
               '"pass":"white","line":"white:0:0","cut":0,"bg":[1,2,3]}]}\n').encode("utf-8")
        data = tileocr.parse_output(b"\xef\xbb\xbf" + out)
        self.assertEqual(data["words"][0]["text"], "х×40")
        self.assertEqual(tileocr.parse_badge(data["words"][0]["text"]), None)  # 'хx40' is not a badge
        self.assertEqual(tileocr.parse_badge("×40"), 40)


if __name__ == "__main__":
    unittest.main(verbosity=1)
