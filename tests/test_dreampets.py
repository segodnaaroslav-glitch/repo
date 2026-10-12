"""DreamPets: правильность цен, лотов и названий (найдено при проверке v5)."""

import json
import tempfile
import threading
import time
import unittest
import unittest.mock
import uuid
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from supreme import dreampets, markets, parser, store

U1 = "2c0af45a-9134-4c09-9b86-502e81ac758e"
U2 = "3a61a47a-fde2-4740-8de8-c214e8aefca8"
U3 = "43948eb3-1e25-4564-8102-65bcaeea06db"


def uid(number):
    return str(uuid.UUID(int=number))


def page(name, price=None, lots=None, extra=""):
    """Страница товара: цена и лоты — в описании страницы."""
    description = f"Купить {name} в ММ2" + (f" от {price} ₽" if price is not None else "") + \
        (f", {lots} лотов" if lots is not None else "")
    return (f'<html><head><title>{name} — купить в ММ2</title><meta name="description" content="{description}">'
            f'</head><body><h1>{name}</h1>{extra}</body></html>')


class Site:
    """Поддельный DreamPets: путь -> тело (str/bytes) или код ответа (int); журнал запросов."""

    def __init__(self, routes=None, delay=None):
        self.routes = dict(routes or {})
        self.delay = dict(delay or {})
        self.log = []
        self.active = self.max_active = 0
        self.lock = threading.Lock()
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                with site.lock:
                    site.log.append(self.path)
                    site.active += 1
                    site.max_active = max(site.max_active, site.active)
                try:
                    time.sleep(site.delay.get(self.path, 0))
                    body = site.routes.get(self.path)
                    if body is None or isinstance(body, int):
                        self.send_response(body or 404)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    data = body if isinstance(body, bytes) else body.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json" if self.path.startswith("/api/")
                                     else "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass
                finally:
                    with site.lock:
                        site.active -= 1

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def product_requests(self):
        return [path for path in self.log if path.startswith("/mm2/product/")]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def links(*products):
    return "".join(f'<a href="/mm2/product/{slug}/{key}">{slug}</a>' for slug, key in products)


class DreamPetsCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        for patch in (unittest.mock.patch.object(markets, "_pause"),
                      unittest.mock.patch.object(markets.fetcher, "find_system_browser", return_value=None),
                      unittest.mock.patch.object(markets.store, "DATA_FILE", self.tmp / "values.json")):
            patch.start()
            self.addCleanup(patch.stop)

    def site(self, routes, **kwargs):
        site = Site(routes, **kwargs)
        self.addCleanup(site.close)
        return site

    @staticmethod
    def adapter(site, cache=None):
        config = {"id": "dreampets", "title": "DreamPets", "kind": "dreampets", "url": site.base + "/mm2/"}
        return markets.DreamPetsAdapter(config, cache if cache is not None else {})

    @staticmethod
    def offers(adapter):
        return {o["name"]: o for o in adapter.fetch()}

    @staticmethod
    def bare(root="https://dreampets.gg"):
        adapter = markets.DreamPetsAdapter({"id": "d", "title": "D", "kind": "dreampets", "url": root + "/mm2/"})
        adapter.root = root
        return adapter


class MissingPagesTests(DreamPetsCase):
    """4, 16: страница 404 или таймаут не держит площадку «прочитанной не целиком»."""

    def test_404_and_timeout_still_complete_and_404_is_dropped(self):
        site = self.site({"/mm2/": links(("harvester", U1), ("seer", U2), ("gemstone", U3)),
                          f"/mm2/product/harvester/{U1}": page("Harvester", "328.98", 674)},
                         delay={f"/mm2/product/gemstone/{U3}": 0.8})
        cache = {}
        with unittest.mock.patch.object(markets.DreamPetsAdapter, "PAGE_TIMEOUT", 0.3):
            adapter = self.adapter(site, cache)
            offers = self.offers(adapter)
        self.assertTrue(adapter.complete)  # можно считать покупки и убирать пропавшие предложения
        products = cache["products"]
        self.assertGreater(products[U3]["at"], 0)  # таймаут: очередь до него дошла
        self.assertNotIn("gemstone", offers)  # данных о нём нет — и не «нет в продаже»
        self.assertEqual((products[U2]["price"], products[U2]["lots"], products[U2]["missing"]), (None, None, 1))
        # Не продаётся, но лоты неизвестны (не 0): исчезнувшая страница — не купленные лоты.
        self.assertEqual((offers["seer"]["available"], offers["seer"]["price"], offers["seer"]["stock"]),
                         (False, None, None))
        for _ in range(markets.DreamPetsAdapter.MISSING_DROP):
            self.adapter(site, cache).fetch()
        self.assertIn(U2, cache["products"])  # 404 подряд, но за минуты (сбой площадки) — запись остаётся
        cache["products"][U2]["missing_since"] -= markets.DreamPetsAdapter.MISSING_TIME
        self.adapter(site, cache).fetch()
        self.assertNotIn(U2, cache["products"])  # 404 подряд больше 12 часов — запись удалена
        self.assertIn(U2, cache["gone"])
        adapter = self.adapter(site, cache)
        self.assertNotIn("seer", self.offers(adapter))  # ссылка на рынке осталась, но запись не вернулась
        self.assertNotIn(U2, cache["products"])
        self.assertTrue(adapter.complete)

        # Раз в GONE_RECHECK страница снятого товара проверяется снова: 404 — опять снят ...
        requests = len(site.product_requests())
        cache["gone"][U2] -= markets.DreamPetsAdapter.GONE_RECHECK
        adapter = self.adapter(site, cache)
        self.assertNotIn("seer", self.offers(adapter))
        self.assertTrue(adapter.complete)
        self.assertEqual(site.product_requests()[requests:].count(f"/mm2/product/seer/{U2}"), 1)
        self.assertNotIn(U2, cache["products"])
        self.assertGreater(cache["gone"][U2], time.time() - 60)
        self.adapter(site, cache).fetch()
        self.assertNotIn(U2, cache["products"])
        # ... а страница снова есть — товар вернулся, хоть цену ему не дал ни один список.
        site.routes[f"/mm2/product/seer/{U2}"] = page("Seer", "17", 82)
        cache["gone"][U2] -= markets.DreamPetsAdapter.GONE_RECHECK
        offers = self.offers(self.adapter(site, cache))
        self.assertEqual((offers["Seer"]["price"], offers["Seer"]["stock"]), (17.0, 82))
        self.assertNotIn(U2, cache["gone"])

    def test_gone_product_comes_back_when_listed_with_a_price(self):
        adapter = self.bare()
        adapter.cache["gone"] = {U2: time.time()}
        products = {}
        self.assertIsNone(adapter._upsert(products, {"name": None}, 0, "slug", f"/mm2/product/seer/{U2}"))
        adapter._upsert(products, {"name": "Seer", "price": 17.0}, 1, "страница рынка", f"/mm2/product/seer/{U2}")
        self.assertEqual(products[U2]["price"], 17.0)
        self.assertNotIn(U2, adapter.cache["gone"])

    def test_delisted_product_is_not_for_sale_then_hidden_then_forgotten(self):
        seer = f'<a href="/mm2/product/seer/{U2}"><p>Seer</p><b>от 17 ₽</b><i>82 лота</i></a>'
        site = self.site({"/mm2/": links(("harvester", U1)) + seer,
                          f"/mm2/product/harvester/{U1}": page("Harvester", "328.98", 674)})
        cache = {}
        self.assertEqual(self.offers(self.adapter(site, cache))["Harvester"]["price"], 328.98)
        site.routes["/mm2/"] = seer
        site.routes.pop(f"/mm2/product/harvester/{U1}")  # снят с продажи: 404
        harvester = self.offers(self.adapter(site, cache))["Harvester"]
        self.assertEqual((harvester["price"], harvester["stock"], harvester["available"]), (None, None, False))

        entry = {"name": "Seer", "price": 17.0, "lots": 82, "at": 1, "url": f"{site.base}/mm2/product/seer/{U2}"}
        adapter = self.adapter(site, cache)
        adapter.root = site.base
        products = {U2: dict(entry, seen=time.time() - adapter.STALE_AFTER - 60)}
        self.assertEqual(adapter._offers(products), [])  # давно никто не подтверждал — данные устарели
        products[U2]["seen"] = time.time()
        self.assertEqual([o["name"] for o in adapter._offers(products)], ["Seer"])
        products[U2]["seen"] = time.time() - adapter.FORGET_AFTER - 60
        adapter._forget(products)
        self.assertEqual(products, {})


class ProductPageTests(unittest.TestCase):
    """5, 10, 14, 31: страница товара."""

    def test_sold_out_page_does_not_take_a_related_product(self):
        html = ('<html><head><title>Harvester — купить в ММ2</title></head><body><header>Бонус при пополнении от 500 ₽'
                '</header><h1>Harvester</h1><p>Нет в наличии</p><h2>Похожие товары</h2>'
                f'<div><a href="/mm2/product/seer/{U2}">Seer</a><span>от 17 ₽</span><span>82 лота</span></div>'
                '</body></html>')
        self.assertEqual(dreampets.parse_product_page(html), {"name": "Harvester", "price": None, "lots": 0,
                                                              "source": "phrase"})  # только надпись
        zero = page("Harvester", "328.98", 0)  # «0 лотов» — не продаётся, хоть цена и написана
        self.assertEqual((dreampets.parse_product_page(zero)["price"], dreampets.parse_product_page(zero)["lots"]),
                         (None, 0))

    def test_banners_sold_counts_and_lot_words_are_not_prices(self):
        def parsed(body):
            result = dreampets.parse_product_page(f"<html><head><title>X</title></head><body>{body}</body></html>")
            return result["price"], result["lots"]

        self.assertEqual(parsed('<div>Бонус при пополнении от 500 ₽</div><h1>Harvester</h1><div>от 328.98 ₽</div>'
                                '<div>674 лотов</div>'), (328.98, 674))
        self.assertEqual(parsed('<h1>Harvester</h1><div>Бонус при пополнении от 500 ₽</div><div>от 328.98 ₽</div>'),
                         (328.98, None))
        self.assertEqual(parsed('<h1>Harvester</h1><p>Последний проданный лот 150 ₽</p>'), (None, None))
        self.assertEqual(parsed('<h1>Harvester</h1><p>Продано 1 250 лотов за месяц</p><p>от 328.98 ₽</p><p>674 лотов</p>'),
                         (328.98, 674))
        self.assertEqual(parsed('<h1>Harvester</h1><p>Продано 1 250 лотов за месяц</p>'), (None, None))
        self.assertEqual(parsed('<h1>Harvester</h1><p>Цена от 328.98 ₽</p>'), (328.98, None))  # «от» — слово

    def test_title_forms(self):
        for title in ("Купить Harvester в ММ2 — DreamPets", "Harvester — купить в ММ2 (Murder Mystery 2)",
                      "Harvester | DreamPets", "Harvester / Жнец — купить в ММ2", "Купить Harvester в MM2"):
            self.assertEqual(dreampets.parse_product_page(f"<title>{title}</title>")["name"], "Harvester", title)
        self.assertIsNone(dreampets.parse_product_page("<title>DreamPets — маркет</title>")["name"])
        h1 = dreampets.parse_product_page("<title>DreamPets</title><h1><span>Chroma</span><span>Harvester</span></h1>"
                                          "<p>Популярность: высокая</p><p>от 900 ₽</p>")
        self.assertEqual((h1["name"], h1["price"]), ("Chroma Harvester", 900.0))

    def test_json_of_another_product_is_not_taken(self):
        related = json.dumps({"related": [{"id": U2, "name": "Seer", "minPrice": 17, "lotsCount": 82}]})
        html = f'<title>Godseye | DreamPets</title><script type="application/json">{related}</script>'
        result = dreampets.parse_product_page(html, f"https://dreampets.gg/mm2/product/godseye/{U1}")
        self.assertEqual((result["name"], result["price"], result["lots"]), ("Godseye", None, None))
        nameless = f'<script type="application/json">{related}</script>'
        self.assertEqual(dreampets.parse_product_page(nameless)["price"], None)  # без названия не угадываем
        own = json.dumps({"product": {"id": U1, "name": "Godseye", "minPrice": 45.5, "lotsCount": 3},
                          "related": [{"id": U2, "name": "Seer", "minPrice": 17, "lotsCount": 82}]})
        result = dreampets.parse_product_page(f'<script type="application/json">{own}</script>',
                                              f"https://dreampets.gg/mm2/product/godseye/{U1}")
        self.assertEqual((result["name"], result["price"], result["lots"]), ("Godseye", 45.5, 3))
        canonical = (f'<link rel="canonical" href="https://dreampets.gg/mm2/product/godseye/{U1}">'
                     f'<script type="application/json">{own}</script>')
        self.assertEqual(dreampets.parse_product_page(canonical)["price"], 45.5)  # адрес — из canonical

    def test_json_without_lot_count_takes_lots_from_description(self):
        ld = json.dumps({"@type": "Product", "name": "Harvester",
                         "offers": {"@type": "AggregateOffer", "lowPrice": "328.98", "priceCurrency": "RUB"}})
        html = (f'<title>Harvester — купить в ММ2</title><meta name="description" content="от 328.98 ₽, 674 лотов">'
                f'<script type="application/ld+json">{ld}</script>')
        self.assertEqual(dreampets.parse_product_page(html)["lots"], 674)


class PageReadingTests(DreamPetsCase):
    """5, 7, 10, 31 через адаптер."""

    def test_sold_out_product_is_not_for_sale(self):
        site = self.site({"/mm2/": links(("harvester", U1)) + f'<a href="/mm2/product/seer/{U2}"><p>Seer</p>'
                                   f'<b>от 17 ₽</b><i>82 лота</i></a>',
                          f"/mm2/product/harvester/{U1}": page("Harvester", extra=(
                              '<p>Нет в наличии</p><h2>Похожие товары</h2><div>Seer от 17 ₽ 82 лота</div>'))})
        cache = {"products": {U1: {"name": "Harvester", "price": 328.98, "lots": 674, "at": 1,
                                   "url": f"{site.base}/mm2/product/harvester/{U1}"}}}
        offers = self.offers(self.adapter(site, cache))
        harvester = offers["Harvester"]
        self.assertEqual((harvester["price"], harvester["stock"], harvester["available"]), (None, 0, False))

    def test_trailing_pages_without_price_do_not_stop_page_reading(self):
        keys = [uid(n) for n in range(1, 21)]
        routes = {"/mm2/": links(*((f"item-{n}", key) for n, key in enumerate(keys, 1)))}
        for n, key in enumerate(keys, 1):
            # 14 страниц с ценой, последние 6 — без цены и без числа лотов
            routes[f"/mm2/product/item-{n}/{key}"] = page(f"Item {n}", 10 + n, 5) if n <= 14 else page(f"Item {n}")
        site = self.site(routes)
        cache = {}
        self.offers(self.adapter(site, cache))
        self.assertLessEqual(cache.get("pages_useless_until", 0), time.time())
        for n, key in enumerate(keys[:14], 1):
            site.routes[f"/mm2/product/item-{n}/{key}"] = page(f"Item {n}", 999, 5)
        offers = self.offers(self.adapter(site, cache))
        self.assertEqual({offers[f"Item {n}"]["price"] for n in range(1, 15)}, {999.0})

    def test_batch_without_any_price_marks_pages_useless(self):
        keys = [uid(n) for n in range(1, 8)]
        routes = {"/mm2/": links(*((f"item-{n}", key) for n, key in enumerate(keys, 1)))}
        routes.update({f"/mm2/product/item-{n}/{key}": '<div id="app"></div>' for n, key in enumerate(keys, 1)})
        site = self.site(routes)
        cache = {}
        with self.assertRaises(markets.fetcher.FetchError):
            self.adapter(site, cache).fetch()
        self.assertGreater(cache["pages_useless_until"], time.time())

    def test_lots_without_price_keep_the_old_price(self):
        site = self.site({"/mm2/": links(("harvester", U1)),
                          f"/mm2/product/harvester/{U1}": page("Harvester", extra="<p>3 лота</p>")})
        cache = {"products": {U1: {"name": "Harvester", "price": 328.98, "lots": 674, "at": 1,
                                   "url": f"{site.base}/mm2/product/harvester/{U1}"}}}
        harvester = self.offers(self.adapter(site, cache))["Harvester"]
        self.assertEqual((harvester["price"], harvester["stock"], harvester["available"]), (328.98, 3, True))

    def test_page_title_never_renames_a_known_product(self):
        site = self.site({"/mm2/": f'<a href="/mm2/product/harvester/{U1}"><p>Harvester</p><span>от 300 ₽</span></a>',
                          f"/mm2/product/harvester/{U1}": page("Seer", "17", 82)})
        cache = {}
        offers = self.offers(self.adapter(site, cache))
        self.assertEqual(list(offers), ["Harvester"])
        self.assertEqual((offers["Harvester"]["price"], offers["Harvester"]["stock"]), (300.0, 82))

    def test_godseye_keeps_its_name_and_price_end_to_end(self):
        related = json.dumps({"related": [{"id": U2, "name": "Seer", "minPrice": 17, "lotsCount": 82}]})
        site = self.site({"/mm2/": links(("godseye", U1)),
                          f"/mm2/product/godseye/{U1}": f'<title>Godseye | DreamPets</title><meta name="description" '
                                                        f'content="от 45.5 ₽, 3 лота"><script type="application/json">'
                                                        f'{related}</script>'})
        cache = {}
        offers = self.offers(self.adapter(site, cache))
        self.assertEqual(list(offers), ["Godseye"])  # не «Seer» из «похожих»
        self.assertEqual((offers["Godseye"]["price"], offers["Godseye"]["stock"]), (45.5, 3))
        self.assertEqual(list(cache["products"]), [U1])


class BlockedTests(DreamPetsCase):
    """8, 19, 24: блокировки и нагрузка."""

    CACHE = {"products": {U1: {"name": "Harvester", "price": 100.0, "lots": 5, "at": 1,
                               "url": f"https://dreampets.gg/mm2/product/harvester/{U1}"}}}

    def cache(self):
        return json.loads(json.dumps(self.CACHE))

    def test_blocked_market_page_is_an_error_and_429_reads_no_pages_in_browser(self):
        site = self.site({"/mm2/": 429})
        cache = self.cache()
        with unittest.mock.patch.object(markets.DreamPetsAdapter, "_browser_pass") as browser:
            with self.assertRaises(markets.fetcher.BlockedError) as caught:
                self.adapter(site, cache).fetch()
        self.assertIn("429", str(caught.exception))
        self.assertEqual(browser.call_args[0][3], False)  # после 429 — без страниц товаров
        self.assertEqual(site.log, ["/mm2/"])  # ни карт сайта, ни страниц товаров
        self.assertEqual(cache["products"][U1]["price"], 100.0)  # кэш на месте

        site.routes["/mm2/"] = 403
        cache["browser_at"] = 0
        with unittest.mock.patch.object(markets.DreamPetsAdapter, "_browser_pass") as browser:
            with self.assertRaises(markets.fetcher.BlockedError):
                self.adapter(site, cache).fetch()
        self.assertEqual(browser.call_args[0][3], True)  # проверку 403 браузер может пройти

    def test_browser_cards_after_block_are_partial_data(self):
        site = self.site({"/mm2/": 403})

        def browser_pass(adapter, base, products, now, read_pages):
            adapter._upsert(products, {"name": "Harvester", "price": 120.0}, now, "браузер", f"/mm2/product/harvester/{U1}")

        with unittest.mock.patch.object(markets.DreamPetsAdapter, "_browser_pass", browser_pass):
            adapter = self.adapter(site, self.cache())
            offers = self.offers(adapter)
        self.assertEqual(offers["Harvester"]["price"], 120.0)
        self.assertIn("не обновлена", adapter.note)

    def test_block_before_anything_fresh_is_an_error(self):
        site = self.site({"/mm2/": links(("harvester", U1), ("seer", U2)), f"/mm2/product/harvester/{U1}": 429,
                          f"/mm2/product/seer/{U2}": page("Seer", 17, 82)})
        cache = self.cache()
        with unittest.mock.patch.object(markets.DreamPetsAdapter, "WORKERS", 1):
            with self.assertRaises(markets.fetcher.BlockedError):
                self.adapter(site, cache).fetch()
        self.assertEqual(site.product_requests(), [f"/mm2/product/harvester/{U1}"])  # дальше не читали

    def test_monitor_pauses_and_keeps_last_ok_time(self):
        path = self.tmp / "values.json"
        data = store.empty_data()
        store.replace_category(data, "godlies", [parser.make_item("Harvester", "godlies", {"value": "100"})], "site")
        store.save(data, path)
        site = self.site({"/mm2/": f'<a href="/mm2/product/harvester/{U1}"><p>Harvester</p><b>от 328.98 ₽</b>'
                                   f'<i>674 лотов</i></a>'})
        config = {"id": "dreampets", "title": "DreamPets", "kind": "dreampets", "url": site.base + "/mm2/"}
        monitor = markets.MarketMonitor(path, [config])
        market = monitor.markets[0]
        monitor.poll(market)
        self.assertEqual(market.status, "ok")
        last_ok = market.last_ok_at
        site.routes["/mm2/"] = 429
        before = markets._now()
        with unittest.mock.patch.object(markets.DreamPetsAdapter, "_browser_pass"):
            monitor.poll(market)
        self.assertEqual(market.status, "error")
        self.assertEqual(market.last_ok_at, last_ok)
        self.assertGreaterEqual(market.next_poll_at - before, timedelta(seconds=markets.BLOCKED_PAUSE))
        self.assertEqual(len(market.offers), 1)  # прошлые цены остаются

    def test_api_address_answering_403_is_not_a_block(self):
        site = self.site({"/mm2/": links(("harvester", U1)), "/api/catalog": 403,
                          f"/mm2/product/harvester/{U1}": page("Harvester", "555", 9)})
        cache = {"api_urls": [site.base + "/api/catalog"]}
        offers = self.offers(self.adapter(site, cache))
        self.assertEqual(offers["Harvester"]["price"], 555.0)  # страницы товаров прочитаны
        self.assertEqual(cache["api_fails"], {site.base + "/api/catalog": 1})
        for _ in range(markets.DreamPetsAdapter.API_FAILS - 1):
            self.adapter(site, cache).fetch()
        self.assertEqual(cache["api_urls"], [])  # адрес без браузера не работает — забыт
        self.assertEqual(cache["api_fails"], {})

    def test_pages_are_read_gently_but_all_of_them_over_several_polls(self):
        keys = [uid(n) for n in range(1, 8)]
        routes = {"/mm2/": links(*((f"item-{n}", key) for n, key in enumerate(keys, 1)))}
        routes.update({f"/mm2/product/item-{n}/{key}": page(f"Item {n}", 10 + n, n) for n, key in enumerate(keys, 1)})
        site = self.site(routes, delay={path: 0.2 for path in routes if path != "/mm2/"})
        cache = {}
        self.assertLessEqual(markets.DreamPetsAdapter.WORKERS, 2)
        with unittest.mock.patch.object(markets.DreamPetsAdapter, "PAGES_PER_POLL", 3):
            for _ in range(3):
                self.adapter(site, cache).fetch()
        self.assertLessEqual(site.max_active, 2)
        self.assertEqual(len(set(site.product_requests())), 7)  # за три опроса — каждая страница


class StopTests(DreamPetsCase):
    """13: закрытие программы прерывает чтение страниц сразу."""

    def test_stop_event_interrupts_page_reading(self):
        keys = [uid(n) for n in range(1, 9)]
        routes = {"/mm2/": links(*((f"item-{n}", key) for n, key in enumerate(keys, 1)))}
        routes.update({f"/mm2/product/item-{n}/{key}": page(f"Item {n}", 10, 1) for n, key in enumerate(keys, 1)})
        site = self.site(routes, delay={path: 3 for path in routes if path != "/mm2/"})
        adapter = self.adapter(site)
        threading.Timer(0.5, adapter.stop_event.set).start()
        started = time.time()
        with self.assertRaises(markets.fetcher.FetchError):
            adapter.fetch()  # ни одной цены не успели — обычная ошибка «нет цен»
        self.assertLess(time.time() - started, 2.5)
        time.sleep(0.2)
        self.assertLessEqual(len(site.product_requests()), markets.DreamPetsAdapter.WORKERS)  # очередь не дочитывали

    def test_monitor_gives_its_stop_event_to_adapters(self):
        config = {"id": "dreampets", "title": "DreamPets", "kind": "dreampets", "url": "https://dreampets.gg/mm2/"}
        monitor = markets.MarketMonitor(self.tmp / "values.json", [config])
        self.assertIs(monitor._adapter(monitor.markets[0]).stop_event, monitor._stop)
        self.assertFalse(self.bare().stop_event.is_set())


class JsonTests(DreamPetsCase):
    """9, 14: данные страниц и API."""

    def test_lots_inside_a_product_are_not_products(self):
        data = {"product": {"id": U1, "name": "Harvester", "minPrice": 328.98, "lotsCount": 674, "lots": [
            {"id": 9001, "name": "Harvester", "price": 328.98}, {"id": 9002, "name": "Harvester", "price": 412.5}]}}
        self.assertEqual([(p["name"], p["price"], p["lots"]) for p in dreampets.walk_products(data)],
                         [("Harvester", 328.98, 674)])
        phantom = {"product": {"id": U1, "name": "Harvester", "minPrice": 328.98, "lotsCount": 674,
                               "variants": [{"id": U2, "name": "Harvester", "price": 412.5}]}}
        adapter = self.bare()
        products = {}
        adapter._absorb_json(phantom, products, 1, "данные страницы")
        self.assertEqual(list(products), [U1])  # лот с uuid — не товар-призрак
        self.assertEqual(products[U1]["price"], 328.98)
        category = {"name": "Godly", "minPrice": 5, "items": [{"name": "Harvester", "price": 328.98},
                                                             {"name": "Seer", "price": 17}]}
        self.assertEqual([p["name"] for p in dreampets.walk_products(category)], ["Godly", "Harvester", "Seer"])

    def test_name_only_match_keeps_the_lowest_price_of_the_poll(self):
        for prices in ((328.98, 412.5), (412.5, 328.98)):
            adapter = self.bare()
            products = {}
            for number, price in enumerate(prices):
                adapter._upsert(products, {"name": "Harvester", "price": price, "id": 9000 + number}, 1, "api")
            self.assertEqual([e["price"] for e in products.values()], [328.98])
        adapter._new_poll()  # следующий опрос: новая цена, а не минимум с прошлой
        adapter._upsert(products, {"name": "Harvester", "price": 500.0}, 2, "api")
        self.assertEqual([e["price"] for e in products.values()], [500.0])
        seer = {}
        adapter._upsert(seer, {"name": "Seer", "price": 20.0, "id": 1}, 3, "api")  # без лотов
        for source in ("api", "данные страницы"):  # тот же товар из двух источников
            adapter._upsert(seer, {"name": "Seer", "price": 17.0, "lots": 82, "id": 2}, 3, source)
        self.assertEqual([(e["price"], e["lots"]) for e in seer.values()], [(17.0, 82)])

    def test_name_lookup_is_fast(self):
        data = {"items": [{"id": n, "name": f"Item {n}", "price": 10 + n, "lotsCount": n} for n in range(1, 1501)]}
        adapter = self.bare()
        products = {}
        started = time.time()
        for _ in range(3):
            adapter._absorb_json(data, products, 1, "api")
        self.assertLess(time.time() - started, 3)
        self.assertEqual(len(products), 1500)

    def test_only_lot_counts_are_lot_counts(self):
        def lots(data):
            return [p["lots"] for p in dreampets.walk_products(data)]

        self.assertEqual(lots({"name": "Harvester", "price": 328.98, "count": 12, "reviews": [{"text": "ok"}]}), [None])
        self.assertEqual(lots({"name": "Harvester", "minPrice": 328.98, "lots": [{"price": 1}] * 20, "lotsTotal": 674}),
                         [674])
        self.assertEqual(lots({"name": "Harvester", "minPrice": 328.98, "lots": [{"price": 1}] * 20}), [None])
        self.assertEqual(lots({"name": "Harvester", "minPrice": 328.98, "lotsCount": "0"}), [0])


class CardTests(DreamPetsCase):
    """11, 12, 15: карточки на странице рынка."""

    def prices(self, html):
        return {c["name"]: (c["price"], c["lots"]) for c in self.bare()._cards(html).values()}

    def test_number_before_price_is_not_thousands(self):
        self.assertEqual(self.prices(f'<a href="/mm2/product/delta/{U1}"><p>Delta</p><i>114</i><b>230,71 ₽</b></a>'),
                         {"Delta": (230.71, None)})
        self.assertEqual(dreampets.parse_card_text("Harvester\n114\n230,71 ₽")["price"], 230.71)
        self.assertEqual(self.prices(f'<a href="/mm2/product/e/{U1}"><p>Echo</p><b>1 299 ₽</b></a>'),
                         {"Echo": (1299.0, None)})

    def test_price_before_the_link_belongs_to_its_card(self):
        html = "".join(f'<div><span>от {price} ₽</span><a href="/mm2/product/{slug}/{key}">{name}</a></div>'
                       for price, slug, key, name in ((100, "harvester", U1, "Harvester"), (200, "seer", U2, "Seer"),
                                                      (300, "xmas", U3, "Xmas")))
        self.assertEqual(self.prices(html), {"Harvester": (100.0, None), "Seer": (200.0, None), "Xmas": (300.0, None)})

    def test_other_links_inside_a_card_do_not_cut_it(self):
        html = (f'<div class="card"><a href="/mm2/product/harvester/{U1}"><img src="/h.png"></a>'
                f'<a href="/mm2/product/harvester/{U1}"><p>Harvester</p></a><a href="/mm2/catalog/godly">Godly</a>'
                f'<a href="/seller/42">Продавец</a><b>от 328.98 ₽</b><i>674 лотов</i></div>'
                f'<div class="card"><a href="/mm2/product/seer/{U2}"><img src="/s.png"></a>'
                f'<a href="/mm2/product/seer/{U2}"><p>Seer</p></a><b>от 17 ₽</b><i>82 лота</i></div>')
        self.assertEqual(self.prices(html), {"Harvester": (328.98, 674), "Seer": (17.0, 82)})

    def test_badges(self):
        self.assertEqual(dreampets.parse_card_text("Chroma\nHarvester\nот 900 ₽")["name"], "Chroma Harvester")
        self.assertEqual(dreampets.parse_card_text("Chroma\nChroma Seer\nот 900 ₽")["name"], "Chroma Seer")
        for badge in ("Knife", "Gun", "MM2", "Pet", "Godly"):
            self.assertEqual(dreampets.parse_card_text(f"{badge}\nHarvester\nот 900 ₽")["name"], "Harvester", badge)

    def test_card_name_does_not_shorten_the_link_name(self):
        adapter = self.bare()
        products = {}
        adapter._upsert(products, {"name": None}, 0, "slug", f"/mm2/product/chroma-harvester/{U1}")
        adapter._upsert(products, {"name": "Harvester", "price": 900.0}, 1, "страница рынка",
                        f"/mm2/product/chroma-harvester/{U1}")
        self.assertEqual(products[U1]["name"], "chroma harvester")
        adapter._upsert(products, {"name": "Eternal III", "price": 20.0}, 1, "страница рынка",
                        f"/mm2/product/eternal-iii/{U2}")
        self.assertEqual(products[U2]["name"], "Eternal III")
        # Старое неверное название (из прошлой версии) исправляется подходящим к ссылке.
        products[U3] = {"name": "Knife", "at": 1, "seen": time.time()}
        adapter._upsert(products, {"name": "Harvester", "price": 300.0}, 1, "страница рынка",
                        f"/mm2/product/harvester/{U3}")
        self.assertEqual(products[U3]["name"], "Harvester")
        adapter._upsert(products, {"name": "Knife", "price": 300.0}, 1, "браузер", f"/mm2/product/harvester/{U3}")
        self.assertEqual(products[U3]["name"], "Harvester")



class ReviewCardTests(DreamPetsCase):
    """Карточки: раздел или продавец перед названием, шапка, пробел между надписями."""

    def test_category_or_seller_link_before_the_title_is_not_the_name(self):
        html = (f'<div class=card><a href=/mm2/catalog/knives>Knives</a><a href=/mm2/product/harvester/{U1}><img></a>'
                f'<a href=/mm2/product/harvester/{U1}><p>Harvester</p></a><b>от 328 ₽</b><i>3 лотов</i></div>'
                f'<div class=card><a href=/seller/ProTrader>ProTrader</a><a href=/mm2/product/seer/{U2}><p>Seer</p></a>'
                f'<b>от 17 ₽</b></div>'
                f'<div class=card><a href=/mm2/catalog/halloween>Halloween 2023</a>'
                f'<a href=/mm2/product/chroma-harvester/{U3}><span>Chroma</span><p>Harvester</p></a><b>от 900 ₽</b></div>')
        adapter = self.bare()
        products = {}
        adapter._absorb_market(html, products, 1)
        self.assertEqual({k: (e["name"], e["price"], e.get("lots")) for k, e in products.items()},
                         {U1: ("Harvester", 328.0, 3), U2: ("Seer", 17.0, None), U3: ("Chroma Harvester", 900.0, None)})

    def test_listing_name_that_does_not_fit_the_link_does_not_rename(self):
        adapter = self.bare()
        products = {}
        adapter._upsert(products, {"name": None}, 0, "slug", f"/mm2/product/harvester/{U1}")
        adapter._upsert(products, {"name": "Knives", "price": 328.0}, 1, "страница рынка", f"/mm2/product/harvester/{U1}")
        self.assertEqual((products[U1]["name"], products[U1]["price"]), ("harvester", 328.0))
        adapter._upsert(products, {"name": "Chroma Harvester", "price": 900.0}, 1, "страница рынка",
                        f"/mm2/product/harvester/{U1}")
        self.assertEqual(products[U1]["name"], "Chroma Harvester")  # длиннее ссылки — можно
        # Новая запись из браузера: ник продавца вместо названия — название из ссылки.
        adapter._upsert(products, {"name": "ProTrader", "price": 17.0}, 1, "браузер", f"/mm2/product/seer/{U2}")
        self.assertEqual(products[U2]["name"], "seer")
        adapter._upsert(products, {"name": "Seer", "price": 17.0}, 1, "браузер", f"/mm2/product/seer/{U2}")
        self.assertEqual(products[U2]["name"], "Seer")

    def test_links_in_the_header_are_not_cards(self):
        html = (f'<div id=__next><header><a href="/">DreamPets</a><div>Бонус при пополнении от 500 ₽</div>'
                f'<div>Только что купили: <a href=/mm2/product/harvester/{U1}>Harvester</a></div></header>'
                f'<main><div class=grid><div class=card><a href=/mm2/product/harvester/{U1}><p>Harvester</p></a>'
                f'<b>от 328.98 ₽</b><i>674 лотов</i></div><div class=card><a href=/mm2/product/seer/{U2}><p>Seer</p></a>'
                f'<b>от 17 ₽</b><i>82 лота</i></div></div></main></div>')
        self.assertEqual(CardTests.prices(self, html), {"Harvester": (328.98, 674), "Seer": (17.0, 82)})
        # Один товар на странице и баннер выше: карточка кончается на своей цене.
        single = (f'<div id=__next><div class=promo>Бонус при пополнении от 500 ₽</div><div class=card>'
                  f'<a href=/mm2/product/harvester/{U1}><p>Harvester</p></a><b>от 328.98 ₽</b></div></div>')
        self.assertEqual(CardTests.prices(self, single), {"Harvester": (328.98, None)})
        self.assertIsNone(dreampets.parse_card_text("DreamPets\nот 500 ₽")["name"])
        # Товар только в шапке: ни цены баннера, ни «Только что купили» в названии.
        ticker = html.split("<main>")[0] + "</div>"
        self.assertEqual(CardTests.prices(self, ticker), {"Harvester": (None, None)})
        # А шапка и подвал внутри <article> — части карточки.
        article = (f'<div class=grid><article><header><a href=/mm2/product/harvester/{U1}>Harvester</a></header>'
                   f'<footer><b>от 328.98 ₽</b> <i>674 лотов</i></footer></article><article><header>'
                   f'<a href=/mm2/product/seer/{U2}>Seer</a></header><footer><b>от 17 ₽</b></footer></article></div>')
        self.assertEqual(CardTests.prices(self, article), {"Harvester": (328.98, 674), "Seer": (17.0, None)})

    def test_spaced_inline_tags_are_separate_labels(self):
        self.assertEqual(CardTests.prices(self, f'<div><a href="/mm2/product/delta/{U1}"><p>Delta</p><i>114</i> '
                                                f'<b>230,71 ₽</b></a></div>'), {"Delta": (230.71, None)})
        # Одна фраза в соседних надписях — по-прежнему одна: «от» + цена, число + «лотов».
        self.assertEqual(CardTests.prices(self, f'<div><a href="/mm2/product/delta/{U1}"><p>Delta</p><span>от</span> '
                                                f'<b>230,71 ₽</b> <i>674</i> <span>лотов</span></a></div>'),
                         {"Delta": (230.71, 674)})
        self.assertEqual(dreampets.parse_product_page('<h1>Delta</h1><p><span>от</span> <b>230,71 ₽</b></p>'
                                                      '<p><i>674</i> <span>лотов</span></p>')["price"], 230.71)


class ReviewPageTextTests(unittest.TestCase):
    """Описание страницы: «бонусы», «скидка», «продажа» и «в продаже» рядом с ценой и лотами."""

    @staticmethod
    def parsed(description, body='<div id="app"></div>'):
        result = dreampets.parse_product_page(f'<html><head><title>Harvester — купить в ММ2</title><meta '
                                              f'name="description" content="{description}"></head><body>{body}'
                                              f'</body></html>')
        return result["price"], result["lots"]

    def test_promo_and_sale_words_elsewhere_in_the_description(self):
        for description in (
                "Купить Harvester в ММ2 от 328.98 ₽, 674 лотов. Моментальная доставка, кэшбэк и бонусы за покупку",
                "Harvester в ММ2 от 328.98 ₽ со скидкой до 10%, 674 лотов",
                "Продажа и покупка Harvester в ММ2 от 328.98 ₽, 674 лотов",
                "Купить Harvester в ММ2 от 328.98 ₽. В продаже 674 лота."):
            self.assertEqual(self.parsed(description), (328.98, 674), description)
        # А рядом с числом эти слова по-прежнему значат не цену и не лоты в продаже.
        self.assertEqual(self.parsed("Бонус при пополнении от 500 ₽. Harvester от 328.98 ₽"), (328.98, None))
        self.assertEqual(self.parsed("Продано 1 250 лотов за месяц, в продаже 674 лота"), (None, 674))
        self.assertEqual(self.parsed("Продажи за месяц: 1 250 лотов"), (None, None))

    def test_in_stock_wording(self):
        self.assertEqual(self.parsed("", "<h1>Harvester</h1><p>В продаже 674 лота</p>"), (None, 674))
        self.assertEqual(self.parsed("Купить Harvester в ММ2. В продаже 0 лотов."), (None, 0))
        self.assertEqual(dreampets.parse_product_page(
            '<title>Harvester — купить в ММ2</title><meta name="description" content="В продаже 0 лотов">')["source"],
            "text")  # «0 лотов» — число, а не только надпись


class ReviewMissingTests(DreamPetsCase):
    """404 — не покупки; блокировки карты сайта и API; перерыв в работе; долгий ответ."""

    def monitor(self, routes):
        path = self.tmp / "values.json"
        data = store.empty_data()
        store.replace_category(data, "godlies", [parser.make_item(name, "godlies", {"value": "100"})
                                                 for name in ("Harvester", "Seer")], "site")
        store.save(data, path)
        site = self.site(routes)
        config = {"id": "dreampets", "title": "DreamPets", "kind": "dreampets", "url": site.base + "/mm2/"}
        monitor = markets.MarketMonitor(path, [config])
        return site, monitor, monitor.markets[0]

    def test_404_is_not_counted_as_bought_lots(self):
        routes = {"/mm2/": links(("harvester", U1), ("seer", U2)),
                  f"/mm2/product/harvester/{U1}": page("Harvester", "328.98", 5),
                  f"/mm2/product/seer/{U2}": page("Seer", "17", 82)}
        site, monitor, market = self.monitor(routes)
        monitor.poll(market)
        self.assertEqual((market.status, market.complete, market.offers["godlies:Seer"]["stock"]), ("ok", True, 82))
        site.routes.pop(f"/mm2/product/seer/{U2}")  # 404: снят или сбой площадки
        monitor.poll(market)
        self.assertEqual(market.status, "ok")
        seer = market.offers["godlies:Seer"]
        self.assertEqual((seer["price"], seer["stock"], seer["available"]), (None, None, False))
        self.assertEqual((market.sales, market.listed), ({}, {}))
        site.routes[f"/mm2/product/seer/{U2}"] = page("Seer", "17", 82)  # вернулся
        monitor.poll(market)
        self.assertEqual(market.offers["godlies:Seer"]["stock"], 82)
        self.assertEqual((market.sales, market.listed), ({}, {}))

    def test_sitemap_refusal_is_not_a_block(self):
        site = self.site({"/mm2/": links(("harvester", U1)), "/sitemap_index.xml": 403,
                          f"/mm2/product/harvester/{U1}": page("Harvester", "328.98", 674)})
        cache = {}
        for _ in range(2):
            adapter = self.adapter(site, cache)
            offers = self.offers(adapter)
            self.assertEqual(offers["Harvester"]["price"], 328.98)  # страницы товаров читаются
            self.assertNotIn("не обновлена", adapter.note)
        self.assertEqual(site.log.count("/sitemap_index.xml"), 1)  # в следующий раз — через час
        self.assertGreater(cache["sitemap_at"], time.time() - 60)
        # 503 карты сайта: остальную карту — в следующий раз, а страницы — сейчас.
        site.routes["/sitemap.xml"] = 503
        cache["sitemap_at"] = 0
        requests = len(site.log)
        offers = self.offers(self.adapter(site, cache))
        self.assertEqual(offers["Harvester"]["price"], 328.98)
        self.assertNotIn("/sitemap_index.xml", site.log[requests:])
        self.assertIn(f"/mm2/product/harvester/{U1}", site.log[requests:])

    def test_relative_sitemap_in_robots(self):
        sitemap = f'<urlset><url><loc>{{base}}/mm2/product/seer/{U2}</loc></url></urlset>'
        site = self.site({"/mm2/": links(("harvester", U1)), "/robots.txt": "User-agent: *\nSitemap: /maps/products.xml",
                          f"/mm2/product/harvester/{U1}": page("Harvester", "328.98", 674),
                          f"/mm2/product/seer/{U2}": page("Seer", "17", 82)})
        site.routes["/maps/products.xml"] = sitemap.format(base=site.base)
        offers = self.offers(self.adapter(site))
        self.assertEqual((offers["Seer"]["price"], offers["Harvester"]["price"]), (17.0, 328.98))
        self.assertIn("/maps/products.xml", site.log)

    def test_api_rate_limit_is_a_pause_not_a_bad_address(self):
        site = self.site({"/mm2/": links(("harvester", U1)), "/api/catalog": 429, "/api/other": 429,
                          f"/mm2/product/harvester/{U1}": page("Harvester", "555", 9)})
        cache = {"api_urls": [site.base + "/api/catalog", site.base + "/api/other"],
                 "products": {U1: {"name": "Harvester", "price": 100.0, "lots": 5, "at": 1,
                                   "url": f"{site.base}/mm2/product/harvester/{U1}"}}}
        with unittest.mock.patch.object(markets.DreamPetsAdapter, "_browser_pass") as browser:
            for _ in range(markets.DreamPetsAdapter.API_FAILS + 1):
                cache["browser_at"] = 0
                with self.assertRaises(markets.fetcher.BlockedError):
                    self.adapter(site, cache).fetch()
        self.assertEqual(browser.call_args[0][3], False)  # после 429 — без страниц товаров
        self.assertEqual(cache["api_urls"], [site.base + "/api/catalog", site.base + "/api/other"])  # не забыт
        self.assertFalse(cache.get("api_fails"))
        self.assertEqual(site.log.count("/api/other"), 0)  # после первого 429 — ни одного запроса больше
        self.assertEqual(site.product_requests(), [])
        self.assertNotIn("/sitemap.xml", site.log)
        self.assertEqual(cache["products"][U1]["price"], 100.0)

    def test_time_while_the_program_was_closed_does_not_hide_prices(self):
        keys = [uid(n) for n in range(1, 31)]
        routes = {"/mm2/": links(*((f"item-{n}", key) for n, key in enumerate(keys, 1)))}
        routes.update({f"/mm2/product/item-{n}/{key}": page(f"Item {n}", 10 + n, n) for n, key in enumerate(keys, 1)})
        site = self.site(routes)
        night = time.time() - 9 * 3600
        cache = {"poll_at": night, "products": {
            key: {"name": f"Item {n}", "price": 10.0 + n, "lots": n, "at": night, "seen": night, "page_at": night,
                  "url": f"{site.base}/mm2/product/item-{n}/{key}"} for n, key in enumerate(keys, 1)}}
        site.routes["/mm2/"] = links(*((f"item-{n}", key) for n, key in enumerate(keys[:2], 1)))  # на рынке — два
        with unittest.mock.patch.object(markets.DreamPetsAdapter, "PAGES_PER_POLL", 3):
            adapter = self.adapter(site, cache)
            offers = self.offers(adapter)
        self.assertEqual(len(offers), 30)  # «данные с прошлого запуска», пока страницы читаются по очереди
        # А не подтверждённый 6 часов работы — по-прежнему скрыт.
        cache["poll_at"] = time.time()
        cache["products"][keys[-1]]["seen"] = time.time() - markets.DreamPetsAdapter.STALE_AFTER - 60
        with unittest.mock.patch.object(markets.DreamPetsAdapter, "PAGES_PER_POLL", 0):
            self.assertNotIn("Item 30", self.offers(self.adapter(site, cache)))

    def test_slow_page_does_not_hold_the_poll(self):
        class Trickle(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/mm2/":
                    body = (f'<a href="/mm2/product/harvester/{U1}"><p>Harvester</p><b>от 328.98 ₽</b></a>'
                            f'<a href="/mm2/product/seer/{U2}">Seer</a>').encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if not self.path.startswith("/mm2/product/"):
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)  # заголовки сразу, тело — по байту
                self.send_header("Content-Length", "1000")
                self.end_headers()
                try:
                    for _ in range(40):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        if done.wait(0.2):  # тест закончился — не тянуть ответ дальше (Windows не рвёт запись сразу)
                            return
                except OSError:
                    pass

        done = threading.Event()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.addCleanup(done.set)
        config = {"id": "dreampets", "title": "DreamPets", "kind": "dreampets",
                  "url": f"http://127.0.0.1:{httpd.server_address[1]}/mm2/"}
        started = time.time()
        with unittest.mock.patch.multiple(markets.DreamPetsAdapter, PAGES_TIME=1, PAGE_TIMEOUT=1):
            adapter = markets.DreamPetsAdapter(config, {})
            offers = {o["name"]: o for o in adapter.fetch()}
        self.assertLess(time.time() - started, 4)  # не 8 секунд, пока сервер отдаёт по байту
        self.assertEqual(offers["Harvester"]["price"], 328.98)


class ReviewPhraseTests(DreamPetsCase):
    """Надпись «Нет в наличии» на заготовке страницы, которую дорисовывает скрипт."""

    def test_phrase_only_pages_keep_listing_prices_and_mark_pages_useless(self):
        keys = [uid(n) for n in range(1, 9)]
        shell = '<h1>Item {n}</h1><div><p>Лотов пока нет</p><p>Нет в наличии</p></div>'
        routes = {"/mm2/": links(*((f"item-{n}", key) for n, key in enumerate(keys, 1)))}
        routes.update({f"/mm2/product/item-{n}/{key}": shell.format(n=n) for n, key in enumerate(keys, 1)})
        site = self.site(routes)
        cache = {"products": {key: {"name": f"Item {n}", "price": 10.0 + n, "lots": 3, "at": 1, "source": "браузер",
                                    "url": f"{site.base}/mm2/product/item-{n}/{key}"}
                              for n, key in enumerate(keys, 1)}}
        cache["products"][keys[0]]["source"] = markets.DreamPetsAdapter.PAGES  # цена была с самой страницы
        with unittest.mock.patch.object(markets.DreamPetsAdapter, "_browser_pass"):
            offers = {o["id"]: o for o in self.adapter(site, cache).fetch()}
        self.assertEqual([offers[key]["price"] for key in keys[1:]], [10.0 + n for n in range(2, 9)])
        self.assertEqual((offers[keys[0]]["price"], offers[keys[0]]["stock"]), (None, 0))
        self.assertGreater(cache.get("pages_useless_until", 0), time.time())  # браузер снова понадобится

    def test_browser_sold_out_page_clears_the_price(self):
        # Так страницу товара передаёт и браузер: lots 0 — цены нет, название страницы не переименовывает.
        adapter = self.bare()
        products = {U1: {"name": "Harvester", "price": 328.98, "lots": 5, "at": 1, "source": "браузер",
                         "url": f"https://dreampets.gg/mm2/product/harvester/{U1}"}}
        adapter._upsert(products, {"name": "Seer", "price": None, "lots": 0, "source": "phrase"}, 2, "браузер", key=U1)
        self.assertEqual([(o["name"], o["price"], o["stock"], o["available"]) for o in adapter._offers(products)],
                         [("Harvester", None, 0, False)])
        adapter._upsert(products, {"name": "Harvester", "price": 300.0, "lots": 0}, 3, "api", key=U1)
        self.assertIsNone(products[U1]["price"])  # 0 лотов — не продаётся, какая бы цена ни была


if __name__ == "__main__":
    unittest.main()
