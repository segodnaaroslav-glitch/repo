import base64
import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from supreme import calc, markets, ocr, parser, server, store


def items():
    return [
        parser.make_item("Harvester", "ancients", {"value": "250", "demand": "3", "rarity": "2"}),
        parser.make_item("Icebreaker", "godlies", {"value": "100", "demand": "5", "rarity": "4"}),
        parser.make_item("Flowerwood Gun", "godlies", {"value": "20", "demand": "2", "rarity": "2"}),
        parser.make_item("Batwing", "godlies", {"value": "1,000,000", "demand": "10", "rarity": "10"}),
    ]


class ParseLinesTests(unittest.TestCase):
    def test_quantities(self):
        self.assertEqual(calc.parse_lines("Harvester x2\n3 Icebreaker\nFlowerwood Gun - 4\n\n---\nFang"),
                         [("Harvester", 2), ("Icebreaker", 3), ("Flowerwood Gun", 4), ("Fang", 1)])
        self.assertEqual(calc.parse_lines("Harvester х2\nIcebreaker 5 шт\nx3 Fang"),
                         [("Harvester", 2), ("Icebreaker", 5), ("Fang", 3)])
        self.assertEqual(calc.parse_lines("3x Fang")[0], ("Fang", 3))
        self.assertEqual(calc.parse_lines("Eternal III")[0], ("Eternal III", 1))  # римские цифры — часть названия


class MatchTests(unittest.TestCase):
    def test_exact_typos_and_unknown(self):
        found, unmatched = calc.match_text("Harvester x2\nHarvestor\nlcebreaker\nBatwing\nInventory", items())
        by_name = {entry["item"]["name"]: entry for entry in found}
        self.assertEqual(by_name["Harvester"]["qty"], 3)  # точное + опечатка складываются
        self.assertIn("Icebreaker", by_name)  # "lcebreaker" — частая ошибка распознавания
        self.assertNotIn("Batwing", by_name)  # секретный — не продаётся
        self.assertIn("Inventory", unmatched)


class CalcEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "values.json"
        data = store.empty_data()
        for item in items():
            store.replace_category(data, item["category"],
                                   [i for i in data["items"] if i["category"] == item["category"]] + [item], "site")
        store.save(data, path)

        class Fake(markets.Adapter):
            def fetch(self):
                return [{"name": "Harvester", "price": 8.83, "currency": "USD", "stock": None,
                         "sales_week": 5, "available": True, "url": "u"}]

        monitor = markets.MarketMonitor(path, [{"id": "starpets", "title": "StarPets", "kind": "fake", "url": "u",
                                                "fee": 0.2}], adapters={"fake": Fake})
        monitor.poll(monitor.markets[0])
        self.httpd = server.make_server(port=0, data_path=path, update=lambda log: None, monitor=monitor)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.port = self.httpd.server_address[1]

    def post(self, body):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        connection.request("POST", "/api/calc", body=json.dumps(body).encode("utf-8"),
                           headers={"Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json"})
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        return response.status, payload

    def test_text(self):
        status, payload = self.post({"text": "Harvester x2\nSomething Else"})
        self.assertEqual(status, 200)
        harvester = payload["items"][0]
        self.assertEqual((harvester["name"], harvester["qty"]), ("Harvester", 2))
        self.assertEqual(harvester["prices"]["starpets"], {"price": 8.83, "currency": "USD", "price_rub": None,
                                                           "price_rub_approx": False, "fee": 0.2, "stock": None})
        self.assertEqual(payload["unmatched"], ["Something Else"])
        self.assertEqual(payload["fees"], {"starpets": 0.2})

    def test_empty_request(self):
        self.assertEqual(self.post({"text": "  "})[0], 400)

    @unittest.skipIf(os.name == "nt", "на Windows распознавание доступно")
    def test_screenshot_off_windows_explains(self):
        status, payload = self.post({"images": [base64.b64encode(b"not an image").decode()]})
        self.assertEqual(status, 400)
        self.assertIn("Windows", payload["error"])


@unittest.skipUnless(ocr.available(), "распознавание есть только в Windows")
class WindowsOcrTests(unittest.TestCase):
    def test_recognizes_drawn_text(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            png = Path(tmp) / "t.png"
            script = (
                "Add-Type -AssemblyName System.Drawing;"
                "$b = New-Object System.Drawing.Bitmap 900,220;"
                "$g = [System.Drawing.Graphics]::FromImage($b); $g.Clear([System.Drawing.Color]::White);"
                "$f = New-Object System.Drawing.Font('Arial', 40);"
                "$g.DrawString('Harvester x2', $f, [System.Drawing.Brushes]::Black, 20, 20);"
                "$g.DrawString('Icebreaker', $f, [System.Drawing.Brushes]::Black, 20, 120);"
                f"$b.Save('{png}', [System.Drawing.Imaging.ImageFormat]::Png)"
            )
            subprocess.run(["powershell.exe", "-NoProfile", "-Command", script], check=True, timeout=60)
            try:
                lines = ocr.recognize(png.read_bytes())
            except ocr.OcrError as error:
                self.skipTest(f"нет движка распознавания на этой машине: {error}")
        found, _ = calc.match_text("\n".join(lines), items())
        self.assertEqual({e["item"]["name"] for e in found}, {"Harvester", "Icebreaker"})


if __name__ == "__main__":
    unittest.main()


class ReviewV4CalcTests(unittest.TestCase):
    def test_more_quantity_forms(self):
        cases = {
            "Harvester 2": ("Harvester", 2), "Harvester 2x": ("Harvester", 2), "Harvester (3)": ("Harvester", 3),
            "Seer (x10)": ("Seer", 10), "Harvester [x2]": ("Harvester", 2), "Seer (10 шт)": ("Seer", 10),
            "Phoenix 2": ("Phoenix", 2), "Fox 2": ("Fox", 2), "Harvester: 3": ("Harvester", 3),
            "Eternal IV": ("Eternal IV", 1),
        }
        for line, expected in cases.items():
            self.assertEqual(calc.parse_lines(line), [expected], line)

    def test_quantity_on_its_own_line(self):
        self.assertEqual(calc.parse_lines("x2\nHarvester\nIcebreaker\nx3"), [("Harvester", 2), ("Icebreaker", 3)])
        found, unmatched = calc.match_text("x2\nHarvester", items())
        self.assertEqual([(e["item"]["name"], e["qty"]) for e in found], [("Harvester", 2)])
        self.assertEqual(unmatched, [])

    def test_name_with_number_is_not_a_quantity(self):
        stock = items() + [parser.make_item("Candy 2", "misc", {"value": "5", "demand": "1", "rarity": "1"})]
        found, _ = calc.match_text("Candy 2\nIcebreaker 2", stock)
        self.assertEqual({e["item"]["name"]: e["qty"] for e in found}, {"Candy 2": 1, "Icebreaker": 2})


class CalcRobustnessTests(CalcEndpointTests):
    def test_ocr_crash_keeps_text_result(self):
        from unittest import mock
        with mock.patch.object(ocr, "recognize", side_effect=FileNotFoundError("no temp")):
            status, payload = self.post({"text": "Icebreaker", "images": [base64.b64encode(b"x").decode()]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"][0]["name"], "Icebreaker")
        self.assertTrue(payload["ocr_errors"])

    def test_ocr_temp_folder_error_is_ocr_error(self):
        from unittest import mock
        with mock.patch.object(ocr, "available", return_value=True), \
                mock.patch.object(ocr.tempfile, "mkdtemp", side_effect=OSError("disk full")):
            with self.assertRaises(ocr.OcrError):
                ocr.recognize(b"x")

    def test_too_big_request_gets_readable_answer(self):
        from unittest import mock
        with mock.patch.object(server, "MAX_CALC_BODY", 1000):
            status, payload = self.post({"text": "Icebreaker", "images": ["A" * 5000]})
        self.assertEqual(status, 413)
        self.assertIn("Скриншоты", payload["error"])


class OcrRowTests(unittest.TestCase):
    def test_several_names_in_one_line(self):
        stock = items() + [parser.make_item("Seer", "godlies", {"value": "5", "demand": "1", "rarity": "1"})]
        found, unmatched = calc.match_text("Harvester x2 Icebreaker Seer\nInventory Flowerwod Gun Trade", stock)
        self.assertEqual({e["item"]["name"]: e["qty"] for e in found},
                         {"Harvester": 2, "Icebreaker": 1, "Seer": 1, "Flowerwood Gun": 1})
        self.assertEqual(unmatched, ["Inventory", "Trade"])


class InventoryTileTests(unittest.TestCase):
    """Плитки инвентаря со скриншота -> предметы (без настоящего распознавания)."""

    def stock(self):
        return items() + [
            parser.make_item("Cowboy", "vintages", {"value": "40", "demand": "2", "rarity": "6"}),
            parser.make_item("Cowboy", "rares", {"value": "3", "demand": "1", "rarity": "3"}),
        ]

    def test_banner_colour_to_rarity(self):
        self.assertEqual(calc.banner_categories([230, 200, 0]), ("vintages",))
        self.assertEqual(calc.banner_categories([220, 60, 180]), ("godlies",))
        self.assertEqual(calc.banner_categories([40, 120, 230]), ("rares",))
        self.assertEqual(calc.banner_categories([225, 225, 225]), ("commons",))
        self.assertEqual(calc.banner_categories(None), ())

    def test_same_item_on_two_screenshots_is_counted_once(self):
        shot = [{"name": "Harvester", "qty": 3, "text": "Harvester", "score": 1.0},
                {"name": "Seer", "qty": 1, "text": "Seer", "score": 1.0}]
        other = [{"name": "Harvester", "qty": 3, "text": "Harvester", "score": 1.0},
                 {"name": "Icebreaker", "qty": 2, "text": "Icebreaker", "score": 1.0}]
        stock = items() + [parser.make_item("Seer", "godlies", {"value": "5", "demand": "1", "rarity": "1"})]
        found, unmatched = calc.match_tiles([shot, other], stock)
        self.assertEqual({e["item"]["name"]: e["qty"] for e in found}, {"Harvester": 3, "Seer": 1, "Icebreaker": 2})
        self.assertEqual(unmatched, [])

    def test_same_name_items_resolved_by_banner_or_left_to_user(self):
        yellow = {"name": "Cowboy", "qty": 40, "text": "Cowboy", "score": 1.0, "bg": [230, 200, 0]}
        grey = {"name": "Cowboy", "qty": 2, "text": "Cowboy", "score": 1.0, "bg": [60, 60, 60]}
        found, _ = calc.match_tiles([[yellow]], self.stock())
        self.assertEqual((found[0]["item"]["category"], found[0]["qty"], found[0]["ambiguous"]), ("vintages", 40, False))
        self.assertEqual([a["category"] for a in found[0]["alternatives"]], ["rares"])
        found, _ = calc.match_tiles([[grey]], self.stock())
        self.assertTrue(found[0]["ambiguous"])  # цвет не подсказал — выберет пользователь


class InventoryEndpointTests(CalcEndpointTests):
    def test_screenshot_of_inventory(self):
        from unittest import mock
        from tests.test_tileocr import grid
        words = grid({(0, 0): ("Harvester", "x3"), (0, 1): ("Icebreaker", None), (0, 2): ("Harvester", "x 3")})
        with mock.patch.object(ocr, "recognize_words", return_value={"width": 800, "height": 600, "words": words}):
            status, payload = self.post({"images": [base64.b64encode(b"png").decode()]})
        self.assertEqual(status, 200)
        self.assertEqual({i["name"]: i["qty"] for i in payload["items"]}, {"Harvester": 6, "Icebreaker": 1})
        self.assertEqual(payload["tiles"], 3)


@unittest.skipUnless(ocr.available(), "распознавание есть только в Windows")
class WindowsInventoryOcrTests(unittest.TestCase):
    """Настоящее распознавание Windows на скриншотах инвентаря MM2."""

    def names(self):
        return json.loads((Path(__file__).parent / "fixtures" / "mm2_names.json").read_text(encoding="utf-8"))

    def test_real_tile_cowboy_x40(self):
        from supreme import tileocr
        raw = (Path(__file__).parent / "fixtures" / "mm2_cowboy_x40.png").read_bytes()
        data = ocr.recognize_words(raw)
        tiles = tileocr.recognize(data, self.names())
        print("\nOCR words:", [(w["text"], w["pass"]) for w in data["words"]][:40], "\ntiles:", tiles)
        cowboy = [t for t in tiles if t["name"] == "Cowboy"]
        self.assertTrue(cowboy, tiles)
        self.assertEqual(cowboy[0]["qty"], 40)

    def test_drawn_inventory_grid(self):
        import subprocess
        from supreme import tileocr
        tiles_spec = [("Harvester", "x3", "#E6C800"), ("Icebreaker", "", "#D63CB4"), ("Seer", "x40", "#2E78E6"),
                      ("Corrupt", "x2", "#DC1E1E"), ("Fang", "", "#32C850"), ("Luger", "x12", "#C8C8C8")]
        with tempfile.TemporaryDirectory() as tmp:
            png = Path(tmp) / "inv.png"
            draws = []
            for index, (name, badge, colour) in enumerate(tiles_spec):
                x, y = 20 + (index % 3) * 139, 20 + (index // 3) * 185
                draws.append(
                    f"$g.FillRectangle((New-Object System.Drawing.SolidBrush ([System.Drawing.ColorTranslator]::FromHtml('#393939'))), {x}, {y}, 130, 170);"
                    f"$g.FillRectangle((New-Object System.Drawing.SolidBrush ([System.Drawing.ColorTranslator]::FromHtml('{colour}'))), {x}, {y + 140}, 130, 30);"
                    f"$p = New-Object System.Drawing.Drawing2D.GraphicsPath; $sf = New-Object System.Drawing.StringFormat; $sf.Alignment = 'Center';"
                    f"$p.AddString('{name}', $ff, 1, 17, (New-Object System.Drawing.RectangleF {x}, {y + 144}, 130, 26), $sf);"
                    f"$g.DrawPath($pen, $p); $g.FillPath([System.Drawing.Brushes]::White, $p);"
                    + (f"$g.DrawString('{badge}', $fb, [System.Drawing.Brushes]::White, {x + 92}, {y + 8});" if badge else "")
                )
            script = (
                "Add-Type -AssemblyName System.Drawing;"
                "$b = New-Object System.Drawing.Bitmap 460, 420; $g = [System.Drawing.Graphics]::FromImage($b);"
                "$g.SmoothingMode = 'AntiAlias'; $g.TextRenderingHint = 'AntiAlias';"
                "$g.Clear([System.Drawing.ColorTranslator]::FromHtml('#1E1E1E'));"
                "$ff = New-Object System.Drawing.FontFamily 'Arial'; $fb = New-Object System.Drawing.Font('Arial', 11);"
                "$pen = New-Object System.Drawing.Pen ([System.Drawing.ColorTranslator]::FromHtml('#3C3200')), 3;"
                + "".join(draws)
                + f"$b.Save('{png}', [System.Drawing.Imaging.ImageFormat]::Png)"
            )
            subprocess.run(["powershell.exe", "-NoProfile", "-Command", script], check=True, timeout=60)
            data = ocr.recognize_words(png.read_bytes())
        tiles = tileocr.recognize(data, self.names())
        print("\nOCR words:", [(w["text"], w["pass"]) for w in data["words"]][:80], "\ntiles:",
              [(t["name"], t["qty"], t["text"]) for t in tiles])
        got = {t["name"]: t["qty"] for t in tiles if t["name"]}
        expected = {"Harvester": 3, "Icebreaker": 1, "Seer": 40, "Corrupt": 2, "Fang": 1, "Luger": 12}
        matched = {name for name, qty in expected.items() if got.get(name) == qty}
        self.assertGreaterEqual(len(matched), 5, f"распознано {got}")
