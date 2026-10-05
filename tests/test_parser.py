import unittest

from supreme import parser

# Вымышленные предметы в том виде, в каком сайт показывает карточки.
BLOCK_PAGE = """
<html><head><title>MM2 Godly Values</title><script>var x = "Value - 999";</script></head>
<body>
<nav><a>Sets</a><a>Uniques</a><a>Godlies</a></nav>
<p>Values Last Updated - October 5th, 2026 at 12:49 PM // Join our Discord</p>
<div class="card">
  <div class="controls">Inv. Controls</div>
  <h3>Alpha Blade</h3>
  <div>Value - <b>1,330</b></div>
  <div>Range - [1,320 - 1,340]</div>
  <div>Stability - Stable</div>
  <div>Demand - 7 Rarity - 4</div>
  <div>Change in Value - (+20) +1.5%</div>
  <div>Origin - Test Event 2024</div>
</div>
<div class="card">
  <div class="controls">Inv. Controls</div>
  <h3>Beta Gun</h3>
  <div><span>Value -</span><span>126K</span></div>
  <div><span>Range -</span><span>N/A</span></div>
  <div><span>Stability -</span><span>Underpaid For</span></div>
  <div><span>Demand -</span><span>2</span><span>Rarity -</span><span>9</span></div>
</div>
<div class="card">
  <h3>Gamma Knife</h3>
  <div>Value - x2 T1 Commons</div>
  <div>Range - N/A</div>
  <div>Demand - 1 Rarity - 1</div>
</div>
<footer>Copyright</footer>
</body></html>
"""


class NumberTests(unittest.TestCase):
    def test_parse_number(self):
        self.assertEqual(parser.parse_number("2,600"), 2600)
        self.assertEqual(parser.parse_number("126K"), 126000)
        self.assertEqual(parser.parse_number("1.5M"), 1500000)
        self.assertEqual(parser.parse_number("0.5"), 0.5)
        self.assertEqual(parser.parse_number("2,600*"), 2600)
        self.assertIsNone(parser.parse_number("x2 T1 Commons"))
        self.assertIsNone(parser.parse_number("Priceless"))
        self.assertIsNone(parser.parse_number(""))
        self.assertIsNone(parser.parse_number(None))

    def test_value_is_first_number_of_range(self):
        self.assertEqual(parser.program_value("1,330", "1320 - 1340"), 1320)
        self.assertEqual(parser.program_value("1,330", "[1,320 - 1,340]"), 1320)
        self.assertEqual(parser.program_value("90", "[85-100]"), 85)
        self.assertEqual(parser.program_value("2,600", ""), 2600)
        self.assertEqual(parser.program_value("2,600", "N/A"), 2600)
        self.assertIsNone(parser.program_value("x2 T1 Commons", ""))


class PageTests(unittest.TestCase):
    def setUp(self):
        self.items, self.updated, self.orphans = parser.parse_category_page(BLOCK_PAGE, "godlies")
        self.by_name = {item["name"]: item for item in self.items}

    def test_finds_all_cards(self):
        self.assertEqual([item["name"] for item in self.items], ["Alpha Blade", "Beta Gun", "Gamma Knife"])

    def test_fields(self):
        alpha = self.by_name["Alpha Blade"]
        self.assertEqual(alpha["value"], 1320)
        self.assertEqual(alpha["value_text"], "1,330")
        self.assertEqual(alpha["range_text"], "1,320 - 1,340")
        self.assertEqual(alpha["demand"], 7)
        self.assertEqual(alpha["rarity"], 4)
        self.assertEqual(alpha["stability"], "Stable")
        self.assertEqual(alpha["change"], "(+20) +1.5%")
        self.assertEqual(alpha["origin"], "Test Event 2024")
        self.assertEqual(alpha["category"], "godlies")

    def test_values_on_next_line(self):
        beta = self.by_name["Beta Gun"]
        self.assertEqual(beta["value"], 126000)
        self.assertEqual(beta["range_text"], "")
        self.assertEqual(beta["stability"], "Underpaid For")
        self.assertEqual((beta["demand"], beta["rarity"]), (2, 9))

    def test_non_numeric_value_kept_as_text(self):
        gamma = self.by_name["Gamma Knife"]
        self.assertIsNone(gamma["value"])
        self.assertEqual(gamma["value_text"], "x2 T1 Commons")

    def test_last_updated(self):
        self.assertEqual(self.updated, "October 5th, 2026 at 12:49 PM")

    def test_script_text_ignored(self):
        self.assertNotIn("999", [item["value_text"] for item in self.items])


class TextTests(unittest.TestCase):
    def test_pasted_text_single_line_cards(self):
        text = "Delta Axe Value - 500 Range - [480 - 520] Stability - Doing Well Demand - 5 Rarity - 3\n"
        items = parser.items_from_text(text, "godlies")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Delta Axe")
        self.assertEqual(items[0]["value"], 480)
        self.assertEqual(items[0]["stability"], "Doing Well")

    def test_chroma_prefix_expanded(self):
        items = parser.items_from_text("C. Epsilon\nValue - 4,000\n", "chromas")
        self.assertEqual(items[0]["name"], "Chroma Epsilon")

    def test_set_name_before_contains(self):
        text = "Zeta Set\nContains -\nZeta Knife\nZeta Gun\nValue - 300\nDemand - 4\n"
        items = parser.items_from_text(text, "sets")
        self.assertEqual(items[0]["name"], "Zeta Set")
        self.assertEqual(items[0]["value"], 300)

    def test_empty_field_does_not_eat_next_name(self):
        text = "Eta Knife\nValue - 10\nAliases -\nTheta Knife\nValue - 20\n"
        items = parser.items_from_text(text, "rares")
        self.assertEqual([item["name"] for item in items], ["Eta Knife", "Theta Knife"])
        self.assertEqual(items[0]["aliases"], "")

    def test_free_text_field_on_next_line(self):
        text = "Eta Knife\nValue - 10\nOrigin -\nTest Event 2023\nDemand - 2\n"
        items = parser.items_from_text(text, "rares")
        self.assertEqual(items[0]["origin"], "Test Event 2023")
        self.assertEqual(items[0]["demand"], 2)

    def test_stability_value_must_fit(self):
        text = "Iota Gun\nValue - 10\nStability -\nKappa Gun\nValue - 20\n"
        items = parser.items_from_text(text, "rares")
        self.assertEqual([item["name"] for item in items], ["Iota Gun", "Kappa Gun"])
        self.assertEqual(items[0]["stability"], "")

    def test_duplicates_get_suffix(self):
        items = parser.items_from_text("Lambda\nValue - 1\nLambda\nValue - 2\n", "misc")
        self.assertEqual([item["name"] for item in items], ["Lambda", "Lambda (2)"])

    def test_no_cards(self):
        self.assertEqual(parser.items_from_text("nothing here", "godlies"), [])


class PopupTests(unittest.TestCase):
    POPUP = (
        '<script>var _svPopup = {"Mu Blade": {"Value": "1,000", "Range": "[950 - 1,050]", '
        '"Demand": "6", "Rarity": "2", "Stability": "Stable", "Origin": "Test <b>2025</b>"}, '
        '"Nu Gun": {"value": "50", "demand": 1}};</script>'
    )

    def test_popup_only_page(self):
        items, _, _ = parser.parse_category_page("<html><body>" + self.POPUP + "</body></html>", "godlies")
        by_name = {item["name"]: item for item in items}
        self.assertEqual(by_name["Mu Blade"]["value"], 950)
        self.assertEqual(by_name["Mu Blade"]["origin"], "Test 2025")
        self.assertEqual(by_name["Nu Gun"]["value"], 50)
        self.assertEqual(by_name["Nu Gun"]["demand"], 1)

    def test_popup_fills_missing_fields(self):
        page = "<html><body>" + self.POPUP + "<div>Mu Blade</div><div>Value - 1,000</div></body></html>"
        items, _, _ = parser.parse_category_page(page, "godlies")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["value"], 950)
        self.assertEqual(items[0]["demand"], 6)

    def test_broken_popup_ignored(self):
        self.assertEqual(parser.parse_popup("<script>var _svPopup = {broken: 1};</script>"), {})


if __name__ == "__main__":
    unittest.main()


class ReviewRegressionTests(unittest.TestCase):
    """Случаи, найденные при проверке кода."""

    def test_cards_inside_links(self):
        page = "<body>" + "".join(
            f'<a class="card" href="#"><span>{name}</span><span>Value - {value}</span>'
            f"<span>Origin - Xmas 2024</span></a>"
            for name, value in (("Alpha Blade", 10), ("Beta Gun", 500), ("Gamma", 20))
        ) + "</body>"
        items, _, orphans = parser.parse_category_page(page, "godlies")
        self.assertEqual([(i["name"], i["value"], i["origin"]) for i in items],
                         [("Alpha Blade", 10, "Xmas 2024"), ("Beta Gun", 500, "Xmas 2024"), ("Gamma", 20, "Xmas 2024")])
        self.assertEqual(orphans, 0)

    def test_inline_tags_without_spaces(self):
        page = ('<div class="card"><span>Alpha</span><span>Value -</span><span>10</span></div>'
                '<div class="card"><span>Beta</span><span>Value -</span><span>20</span>'
                '<span>Stability -</span><span>Stable</span><span>Demand -</span><span>N/A</span>'
                '<span>Rarity -</span><span>9</span></div>')
        items, _, _ = parser.parse_category_page(page, "godlies")
        self.assertEqual([(i["name"], i["value"]) for i in items], [("Alpha", 10), ("Beta", 20)])
        self.assertEqual((items[1]["stability"], items[1]["demand"], items[1]["rarity"]), ("Stable", None, 9))

    def test_value_written_as_range(self):
        self.assertEqual(parser.items_from_text("Alpha\nValue - 1320 - 1340\n", "godlies")[0]["value"], 1320)
        self.assertEqual(parser.items_from_text("Alpha Value - 1,320 - 1,340 Demand - 5\n", "godlies")[0]["value"], 1320)

    def test_decimal_suffix_has_no_float_noise(self):
        self.assertEqual(parser.parse_number("16.1K"), 16100)
        self.assertEqual(parser.parse_number("4.1M"), 4100000)
        self.assertEqual(parser.parse_number("2.01K"), 2010)
        self.assertEqual(parser.program_value("", "[16.1K - 16.6K]"), 16100)

    def test_empty_field_before_set(self):
        text = "Alpha Set\nContains - A, B\nValue - 300\nAliases -\nBeta Set\nContains -\nBeta Knife\nBeta Gun\nValue - 500\n"
        items = parser.items_from_text(text, "sets")
        self.assertEqual([(i["name"], i["value"], i["aliases"]) for i in items], [("Alpha Set", 300, ""), ("Beta Set", 500, "")])

    def test_empty_origin_before_ui_text(self):
        items = parser.items_from_text("Alpha\nValue - 1\nOrigin -\nInv. Controls\nBeta\nValue - 2\n", "rares")
        self.assertEqual([(i["name"], i["origin"]) for i in items], [("Alpha", ""), ("Beta", "")])

    def test_other_range_formats(self):
        for text in ("1,320 − 1,340", "[1,320 - 1,340]*", "(1,320 - 1,340)", "~1,320 - 1,340", "1,320 ~ 1,340", "1320-1340"):
            self.assertEqual(parser.program_value("1,330", text), 1320, text)
        self.assertEqual(parser.program_value("1,330", "x2 T1 Commons"), 1330)

    def test_value_on_next_line_with_prefix(self):
        self.assertEqual(parser.items_from_text("Alpha\nValue -\n~1,330\n", "rares")[0]["value"], 1330)

    def test_page_without_head_end_tag(self):
        page = "<html><head><title>MM2 Values</title><meta charset=utf-8><body><div>Alpha</div><div>Value - 10</div>"
        items, _, _ = parser.parse_category_page(page, "rares")
        self.assertEqual([(i["name"], i["value"]) for i in items], [("Alpha", 10)])

    def test_last_updated_in_separate_blocks(self):
        page = "<p>Values Last Updated</p><p>-</p><p>October 5th, 2026</p><p>at 12:49 PM</p>"
        self.assertEqual(parser.find_last_updated(parser.html_to_text(page)), "October 5th, 2026 at 12:49 PM")

    def test_badge_is_not_a_name(self):
        items = parser.items_from_text("Alpha Knife\nNEW\nValue - 10\n", "godlies")
        self.assertEqual(items[0]["name"], "Alpha Knife")

    def test_popup_range_as_list_of_strings(self):
        page = '<script>var _svPopup = {"Mu": {"value": "1,000", "range": ["950", "1,050"]}};</script>'
        items, _, _ = parser.parse_category_page(page, "godlies")
        self.assertEqual(items[0]["value"], 950)

    def test_orphan_values_are_counted(self):
        _, orphans = parser.parse_cards("Value - 1\nValue - 2\nAlpha\nValue - 3\n")
        self.assertEqual(orphans, 2)

    def test_bom_removed_from_name(self):
        self.assertEqual(parser.items_from_text("﻿Alpha\nValue - 1\n", "rares")[0]["name"], "Alpha")
