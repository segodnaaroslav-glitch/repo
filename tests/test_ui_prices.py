"""Цены площадок в окне программы: настоящая страница в скрытом Edge/Chrome.

Сервер программы с поддельными площадками и курсом; проверяется то, что видит
пользователь: карточки, «Площадки», окно предмета и калькулятор продажи.
"""

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from supreme import cdp, fetcher, markets, parser, server, store


def _test_browser():
    path = os.environ.get("MM2_TEST_BROWSER") or fetcher.find_system_browser()
    if not path and os.path.exists("/opt/pw-browsers/chromium"):
        path = "/opt/pw-browsers/chromium"
    return path


# StarPets: свои рубли (курс площадки ~80 ₽/$); Fang — только рубли, Flowerwood Gun — только доллары.
STARPETS = [
    {"name": "Harvester", "price": 8.83, "price_rub": 750.0},
    {"name": "Icebreaker", "price": 2.0, "price_rub": 160.0},
    {"name": "Luger", "price": 1.3, "price_rub": 104.0},
    {"name": "Fang", "price": None, "price_rub": 120.0},
    {"name": "Flowerwood Gun", "price": 3.0, "price_rub": None},
]
# DreamPets — рубли; у Candy лоты есть, а цена не прочитана.
DREAMPETS = [
    {"name": "Harvester", "price": 820.0, "stock": 5},
    {"name": "Icebreaker", "price": 150.0, "stock": 3},
    {"name": "Luger", "price": 50.0, "stock": 2},
    {"name": "Candy", "price": None, "stock": 3},
]


class FakeStarPets(markets.Adapter):
    def fetch(self):
        return [dict(o, currency="USD", price_rub_approx=False, stock=None, sales_week=None, available=True, url="u")
                for o in STARPETS]


class FakeDreamPets(markets.Adapter):
    def fetch(self):
        return [dict(o, currency="RUB", sales_week=None, available=True, url="u") for o in DREAMPETS]


class FakeRate:
    usd_rub = None

    def status(self):
        return {"usd_rub": self.usd_rub, "updated_at": None, "source": "Rapira, USDT/RUB", "error": None}


@unittest.skipUnless(_test_browser(), "нет Edge/Chrome для проверки страницы")
class PriceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = Path(cls.tmp.name) / "values.json"
        data = store.empty_data()
        rows = [("Harvester", "ancients", "250"), ("Icebreaker", "godlies", "100"), ("Luger", "godlies", "10"),
                ("Fang", "godlies", "30"), ("Flowerwood Gun", "godlies", "20"), ("Candy", "godlies", "40")]
        for category in {category for _, category, _ in rows}:
            store.replace_category(data, category, [
                parser.make_item(name, category, {"value": value, "demand": "5", "rarity": "5"})
                for name, cat, value in rows if cat == category], "site")
        store.save(data, path)
        configs = [{"id": "starpets", "title": "StarPets", "kind": "starpets", "url": "u", "fee": 0.2},
                   {"id": "dreampets", "title": "DreamPets", "kind": "dreampets", "url": "u", "fee": 0.1}]
        monitor = markets.MarketMonitor(path, configs, adapters={"starpets": FakeStarPets, "dreampets": FakeDreamPets})
        for market in monitor.markets:
            monitor.poll(market)
        cls.monitor = monitor
        cls.rate = FakeRate()
        cls.httpd = server.make_server(port=0, data_path=path, update=lambda log: None, monitor=monitor, rate=cls.rate)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}/"
        try:
            cls.browser = cdp.Browser(_test_browser())
        except Exception:
            cls.httpd.shutdown()
            cls.httpd.server_close()
            cls.tmp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.rate.usd_rub = None
        self.js("try { localStorage.clear(); } catch (e) {} window.__stale = true")
        self.browser.call("Page.navigate", {"url": self.url})
        self.wait("!window.__stale && typeof state !== 'undefined' && state.data.items.length > 0")

    def js(self, expression):
        """Выполнить на странице; неразрывные пробелы ru-RU («1 611,00») — обычные."""
        value = self.browser.evaluate(expression)
        if isinstance(value, str):
            return value.replace("\u00a0", " ").replace("\u202f", " ")
        if isinstance(value, list):
            return [v.replace("\u00a0", " ").replace("\u202f", " ") if isinstance(v, str) else v for v in value]
        return value

    def wait(self, expression, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.js(f"Boolean({expression})"):
                    return
            except cdp.BrowserError:
                pass  # страница ещё загружается
            time.sleep(0.1)
        self.fail(f"не дождались: {expression}")

    def calc(self, text):
        self.js(f"(async () => {{ $('calcText').value = {json.dumps(text)}; await runCalc(); }})()")
        return self.js("[...document.querySelectorAll('#calcTotals > *')].map((n) => n.textContent)")

    def test_site_time_in_local_zone(self):
        # Дата обновления сайта — по времени пользователя, как показывает сам сайт.
        self.browser.call("Emulation.setTimezoneOverride", {"timezoneId": "Europe/Moscow"})
        self.addCleanup(self.browser.call, "Emulation.setTimezoneOverride", {"timezoneId": ""})
        self.assertEqual(self.js("siteTime('October 11th, 2026 at 7:08 PM')"), "12 октября 2026 г. в 02:08")
        self.browser.call("Emulation.setTimezoneOverride", {"timezoneId": "Europe/Kaliningrad"})
        self.assertEqual(self.js("siteTime('October 11th, 2026 at 7:08 PM')"), "12 октября 2026 г. в 01:08")
        self.assertIsNone(self.js("siteTime('скоро')"))
        self.js("state.data.site_last_updated = 'October 11th, 2026 at 7:08 PM'; renderHeader()")
        self.assertIn("2026", self.js("$('siteUpdated').textContent"))
        self.assertIn("October 11th, 2026 at 7:08 PM", self.js("$('siteUpdated').title"))

    def test_ruble_only_price_is_for_sale(self):
        # Только рублёвая цена StarPets — тоже «продаётся»: строка в «Площадках» и выплата.
        rows = self.js("[...document.querySelectorAll('#mkBody tr')].map((tr) => "
                       "[...tr.children].map((td) => td.textContent).join('|'))")
        self.assertIn("|120,00 ₽|96,00 ₽|", next(row for row in rows if "Fang|" in row))
        self.js("openDrawer('godlies:Fang')")
        tile = self.js("document.querySelector('#drawerBody .tile.starpets').textContent")
        self.assertIn("Вы получите (−20%)96,00 ₽", tile)

    def test_lots_without_price_are_not_sold_out(self):
        card = self.js("document.querySelector('#cardsGrid .card[data-key=\"godlies:Candy\"] .mp.dreampets').textContent")
        self.assertEqual(card, "DreamPetsцена неизвестна · 3 лот.")
        self.js("openDrawer('godlies:Candy')")
        tile = self.js("document.querySelector('#drawerBody .tile.dreampets').textContent")
        self.assertIn("цена неизвестна", tile)
        self.assertNotIn("нет в продаже", tile)
        self.assertIn("Candy", self.js("[...document.querySelectorAll('#mkBody tr')].map((tr) => tr.textContent).join()"))
        totals = self.calc("Candy\nIcebreaker")
        self.assertIn("цена неизвестна (не в сумме): 1 шт.", totals[1])
        self.assertNotIn("не продаётся там", totals[1])
        self.assertIn("цена неизвестна", self.js("$('calcBody').textContent"))
        # DreamPets и без Candy больше: 150×0.9 = 135 ₽ против 160×0.8 = 128 ₽.
        self.assertTrue(any("Выгоднее продать на DreamPets — больше как минимум на 7,00 ₽" in t for t in totals), totals)
        # DreamPets меньше (Luger: 45 ₽ против 83,20 ₽), но без цены Candy — какая выгоднее, не понять.
        totals = self.calc("Candy\nLuger")
        self.assertTrue(any("не понять: на DreamPets у 1 шт. цена неизвестна" in t for t in totals), totals)
        self.assertFalse(any("Выгоднее" in t for t in totals), totals)

    def test_totals_show_market_sum_on_top_and_payout_below(self):
        self.rate.usd_rub = 80.0
        self.js("poll()")
        self.calc("Harvester x2\nIcebreaker")
        dream = self.js("[...document.querySelectorAll('#calcTotals .total.dreampets > *')].map((n) => n.textContent)")
        self.assertEqual(dream[0], "DreamPets — по ценам площадки")
        self.assertTrue(dream[1].startswith("1 790,00 ₽"), dream)  # 820×2 + 150 — без комиссии
        self.assertEqual(dream[2], "вы получите (−10%)")
        self.assertTrue(dream[3].startswith("1 611,00 ₽"), dream)  # × 0.9

    def test_totals_without_rate_are_not_partial(self):
        totals = self.calc("Harvester x2\nIcebreaker\nFang\nFlowerwood Gun\nLuger")
        star, dream = totals[0], totals[1]
        # Как у DreamPets: главное — рубли StarPets; без курса у Flowerwood Gun только доллары.
        # Рубли: (750×2 + 160 + 120 + 104) × 0.8 = 1 507,20 ₽; доллары без рублей: 3 × 0.8 = $2.40.
        self.assertIn("1 507,20 ₽ + $2.40", star)
        self.assertNotIn("$0.00", dream)
        self.assertIn("1 656,00 ₽", dream)  # (820×2 + 150 + 50) × 0.9
        self.assertNotIn("$", dream)
        self.assertIn("не продаётся там: 2 шт.", dream)
        # Курс пришёл — калькулятор пересчитывается сам, без «Посчитать».
        self.rate.usd_rub = 80.0
        self.js("poll()")
        self.wait("!document.querySelector('#calcTotals').textContent.includes('Нет курса')")
        star = self.js("document.querySelector('#calcTotals .total.starpets .total-net').textContent")
        self.assertEqual(star, "≈1 699,20 ₽ · ≈$21.24")  # 1 507,20 + 2.4×80; 1 507,20/80 + 2.4
        units = self.js("[...document.querySelectorAll('#calcBody tr')].map((tr) => tr.children[2].textContent)")
        self.assertIn("120,00 ₽ · ≈$1.50", units)

    def test_rate_arrival_while_typing_quantity(self):
        self.calc("Fang\nLuger")
        self.js("switchTab('calc'); document.querySelector('#calcBody input.qty').focus()")
        self.rate.usd_rub = 80.0
        self.js("poll()")
        # Поле количества не пересоздаётся под курсором, а итог уже по курсу.
        self.assertTrue(self.js("document.activeElement === document.querySelector('#calcBody input.qty')"))
        self.assertEqual(self.js("document.querySelector('#calcBody tr').children[2].textContent"), "120,00 ₽")
        self.assertEqual(self.js("document.querySelector('#calcTotals .total.starpets .total-net').textContent"),
                         "179,20 ₽ · ≈$2.24")  # (120 + 104) × 0.8; рубли по курсу 80
        self.js("document.activeElement.blur()")
        self.js("poll()")
        self.assertEqual(self.js("document.querySelector('#calcBody tr').children[2].textContent"), "120,00 ₽ · ≈$1.50")

    def test_rubles_compared_without_rate(self):
        totals = self.calc("Harvester x2\nIcebreaker")
        # StarPets: (750×2 + 160) × 0.8 = 1 328 ₽; DreamPets: (820×2 + 150) × 0.9 = 1 611 ₽.
        self.assertTrue(any("Выгоднее продать на DreamPets — больше на 283,00 ₽" in text for text in totals), totals)

    def test_price_sort_uses_site_rate_without_rapira(self):
        self.js("state.mkSort = 'starpets:asc'; state.mkOnlyListed = false; renderMarketsTab()")
        names = self.js("[...document.querySelectorAll('#mkBody .item-cell > span')].map((n) => n.textContent)")
        # Fang 120 ₽ ≈ $1.50 по курсу StarPets (80 ₽/$), а не $1.20 (рубли / 100).
        self.assertEqual(names[:5], ["Luger", "Fang", "Icebreaker", "Flowerwood Gun", "Harvester"])

    def test_rate_arrival_redraws_prices(self):
        before = self.js("document.querySelector('#cardsGrid .card[data-key=\"godlies:Luger\"] .mp.starpets').textContent")
        self.assertEqual(before, "StarPets104,00 ₽")  # рубли StarPets; доллары — когда будет курс
        fang = "document.querySelector('#cardsGrid .card[data-key=\"godlies:Fang\"] .mp.starpets').textContent"
        self.assertEqual(self.js(fang), "StarPets120,00 ₽")
        self.rate.usd_rub = 100.0
        self.js("poll()")
        self.wait(f"{fang}.includes('$')")
        self.assertEqual(self.js(fang), "StarPets120,00 ₽ · ≈$1.20")
        luger = "document.querySelector('#cardsGrid .card[data-key=\"godlies:Luger\"] .mp.starpets').textContent"
        self.assertEqual(self.js(luger), "StarPets104,00 ₽ · ≈$1.04")  # по курсу Rapira, а не $1.30 самой StarPets



    def test_not_on_sale_row_offers_a_product_link(self):
        # Нет на DreamPets — рядом кнопка «указать ссылку»: окно предмета, поле ссылки открыто.
        self.calc("Flowerwood Gun")
        self.js("switchTab('calc')")
        cell = self.js("document.querySelector('#calcBody tr').children[4].textContent")
        self.assertEqual(cell, "нет в продажеуказать ссылку")
        self.assertNotIn("указать ссылку", self.js("document.querySelector('#calcBody tr').children[2].textContent"))
        self.js("document.querySelector('#calcBody tr').children[4].querySelector('button').click()")
        self.wait("!$('drawer').classList.contains('hidden')")
        self.wait("document.activeElement && document.activeElement.classList.contains('link-url-input')")
        self.assertTrue(self.js("document.querySelector('#drawerBody .tile.dreampets details.link-url').open"))
        url = "https://dreampets.gg/mm2/product/flowerwood-gun/2c0af45a-9134-4c09-9b86-502e81ac758e"
        self.addCleanup(self.monitor.link, "dreampets", "godlies:Flowerwood Gun", None)
        self.js(f"document.activeElement.value = {json.dumps(url)}; "
                "document.querySelector('#drawerBody .tile.dreampets details.link-url button').click()")
        self.wait("document.querySelector('#toasts').textContent.includes('Запомнил')")
        market = next(m for m in self.monitor.markets if m.config["id"] == "dreampets")
        self.assertEqual(market.links["godlies:Flowerwood Gun"], "2c0af45a-9134-4c09-9b86-502e81ac758e")
        self.assertEqual(market.urls["2c0af45a-9134-4c09-9b86-502e81ac758e"], url)

    def test_remote_access_section(self):
        # Доступ с другого компьютера: выключен; пароль и включение — до перезапуска программы.
        self.js("switchTab('import')")
        self.wait("$('remoteBox').textContent.includes('Выключен')")
        self.js("const form = document.querySelector('#remoteBox form'); form.querySelector('input').value = 'secret-pass';"
                "form.requestSubmit()")
        self.wait("$('remoteBox').textContent.includes('Включится после перезапуска')")
        self.assertIn("Выключить доступ", self.js("$('remoteBox').textContent"))
        self.js("[...document.querySelectorAll('#remoteBox button')].find((b) => b.textContent === 'Выключить доступ').click()")
        self.wait("$('remoteBox').textContent.includes('Выключен')")

if __name__ == "__main__":
    unittest.main()
