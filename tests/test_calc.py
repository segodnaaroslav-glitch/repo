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
