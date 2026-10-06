"""Торговые площадки: цены, предложения и продажи MM2-предметов.

Каждая площадка опрашивается в фоне раз в несколько минут. По её данным для
каждого предмета считается своя ликвидность (продажи за неделю, число
предложений, покупки между проверками), а общая ликвидность — среднее Supreme
Values и всех площадок, где предмет нашёлся.

Площадки описаны в DEFAULT_MARKETS. Свой список можно задать файлом
data/markets.json (такой же формат: список словарей id/title/kind/url).
"""

import html as html_lib
import json
import math
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import fetcher, liquidity, parser, store

POLL_EVERY = 120                 # секунд между опросами одной площадки
MAX_BACKOFF = 30 * 60            # при ошибках — реже, но не реже раза в 30 минут
BLOCKED_PAUSE = 15 * 60          # после ответа «слишком много запросов» / «доступ запрещён»
HISTORY_HOURS = 24
MAX_PAGES = 40

# Площадки по умолчанию. kind: "starpets" — API StarPets; "shop" — интернет-магазин
# (сначала Shopify /products.json, затем WooCommerce Store API, затем разметка
# товаров JSON-LD на страницах из "pages").
DEFAULT_MARKETS = [
    # StarPets: данные обновляются у них раз в ~4 минуты, а после ~50 запросов подряд
    # API блокирует адрес на 10 минут — поэтому опрос раз в 5 минут.
    {"id": "starpets", "title": "StarPets", "kind": "starpets", "url": "https://starpets.gg/mm2", "interval": 300},
    {"id": "dreampets", "title": "DreamPets", "kind": "dreampets", "url": "https://dreampets.gg/mm2/", "interval": 180},
    {"id": "eldorado", "title": "Eldorado", "kind": "eldorado", "url": "https://www.eldorado.gg/mm2-shop/i/204-2-0",
     "interval": 180},
]


# --- HTTP ---------------------------------------------------------------------

def _request(url, data=None, headers=None, timeout=30):
    all_headers = dict(fetcher.HEADERS)
    all_headers["Accept"] = "application/json, text/html;q=0.9, */*;q=0.8"
    if data is not None:
        all_headers["Content-Type"] = "application/json"
    all_headers.update(headers or {})
    body = json.dumps(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(url, data=body, headers=all_headers, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as error:
        if error.code in fetcher.BLOCK_STATUSES:
            raise fetcher.BlockedError(f"площадка не пускает программу (HTTP {error.code})") from error
        raise fetcher.FetchError(f"HTTP {error.code}: {url}") from error
    except (urllib.error.URLError, OSError) as error:
        raise fetcher.NetworkError(f"нет соединения ({error}): {url}") from error
    return raw.decode(charset, "replace")


def get_json(url, **kwargs):
    text = _request(url, **kwargs)
    try:
        return json.loads(text)
    except ValueError as error:
        raise fetcher.FetchError(f"ответ не JSON: {url}") from error


def post_json(url, data, **kwargs):
    text = _request(url, data=data, **kwargs)
    try:
        return json.loads(text)
    except ValueError as error:
        raise fetcher.FetchError(f"ответ не JSON: {url}") from error


# --- сопоставление названий --------------------------------------------------

_NOISE_RE = re.compile(
    r"\b(mm2|murder\s+mystery\s*2?|roblox|godly|ancient|vintage|legendary|uncommon|common|unique|"
    r"knife|gun|weapon|item|pet)\b",
    re.I,
)


def market_key(name):
    """Ключ названия товара для поиска в списке Supreme Values."""
    text = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", str(name))  # "(Godly Knife)", "[MM2]"
    text = re.sub(r"^c\.\s*", "chroma ", text.strip(), flags=re.I)
    text = _NOISE_RE.sub(" ", text)
    return parser.name_key(text)


def build_index(items):
    """Поиск предметов Supreme Values по названию товара на площадке.

    Точное название ищется первым. Упрощённый ключ (без "Gun", "Knife", "(Godly)")
    используется, только если он однозначен: "Flowerwood" и "Flowerwood Gun" —
    разные предметы. Секретные и Untradables не сопоставляются.
    """
    exact, loose, ambiguous = {}, {}, set()
    for item in items:
        if item.get("secret") or item.get("category") == "untradables":
            continue
        exact.setdefault(parser.name_key(item["name"]), item)
        key = market_key(item["name"])
        if not key:
            continue
        if key in loose and loose[key] is not item:
            ambiguous.add(key)
        else:
            loose.setdefault(key, item)
    for key in ambiguous:
        loose.pop(key, None)
    return {"exact": exact, "loose": loose}


def find_item(index, offer):
    """Предмет Supreme Values для товара площадки или None."""
    name = str(offer.get("name") or "")
    kind = str(offer.get("kind") or "").lower()
    if kind in ("gun", "knife", "pet"):  # "Flowerwood" с типом Gun — это "Flowerwood Gun"
        item = index["exact"].get(parser.name_key(f"{name} {kind}"))
        if item:
            return item
    return index["exact"].get(parser.name_key(name)) or index["loose"].get(market_key(name))


def item_id(item):
    return f"{item['category']}:{item['name']}"


# --- оценка ликвидности на площадке ------------------------------------------

NO_DATA = "none"


def _level(score):
    if score >= liquidity.LIQUID_FROM:
        return liquidity.LIQUID
    return liquidity.MEDIUM if score >= liquidity.MEDIUM_FROM else liquidity.ILLIQUID


def assess_offer(offer, sold_24h):
    """Балл 0–100 и причины по данным одной площадки.

    Если площадка не показывает ни продаж, ни количества, ни места по
    популярности, балла нет (score=None): «нет данных» — не то же самое, что 0.
    """
    if offer.get("available") is False:
        return {"score": None, "level": NO_DATA, "reasons": ["сейчас нет в продаже"]}
    reasons = []
    score = None
    if offer.get("sales_week") is not None:
        sales = max(0, offer["sales_week"])
        score = min(100.0, 25 * math.log2(1 + sales))
        reasons.append(f"продаж за неделю: {sales}")
    elif offer.get("stock") is not None:
        stock = max(0, offer["stock"])
        score = min(100.0, 20 * math.log2(1 + stock))
        reasons.append(f"в продаже: {stock} шт.")
    elif offer.get("popularity") is not None:
        # Место в списке «популярные» самой площадки: верх списка — ходовые предметы.
        score = 80 * (1 - offer["popularity"])
        reasons.append(f"место по популярности на площадке: верхние {max(1, round(offer['popularity'] * 100))}%")
    if sold_24h:
        score = min(100.0, (score or 0) + min(40, 10 * sold_24h))
        reasons.append(f"куплено за сутки (по проверкам): {sold_24h}")
    if score is None:
        return {"score": None, "level": NO_DATA, "reasons": ["есть в продаже, но площадка не показывает продажи"]}
    score = int(round(score))
    return {"score": score, "level": _level(score), "reasons": reasons}


def combine(item, market_infos):
    """Общая ликвидность: среднее Supreme Values и площадок, где есть данные."""
    supreme = item.get("liquidity") or liquidity.assess(item)
    if supreme["level"] in (liquidity.SECRET, liquidity.UNTRADABLE):
        return dict(supreme, sources=1)
    scored = [info for info in market_infos if info.get("score") is not None]
    scores = [supreme["score"]] + [info["score"] for info in scored]
    score = int(round(sum(scores) / len(scores)))
    reasons = [f"Supreme Values: {supreme['score']}"] + [
        f"{info['market_title']}: {info['score'] if info.get('score') is not None else info['reasons'][0]}"
        for info in market_infos
    ]
    return {"score": score, "level": _level(score), "reasons": reasons, "sources": len(scores)}


# --- адаптеры площадок --------------------------------------------------------

class Adapter:
    def __init__(self, config, cache=None):
        self.config = config
        self.cache = cache if cache is not None else {}  # живёт между опросами и перезапусками
        self.note = ""  # пояснение для окна программы
        # False, пока адаптер не прочитал площадку целиком (читает её частями):
        # тогда старые данные не выбрасываются, а покупки не считаются.
        self.complete = True

    def fetch(self):
        """Список предложений: {"name", "price", "currency", "stock", "sales_week", "available", "url"}."""
        raise NotImplementedError


def _price(value):
    try:
        number = float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None  # 0 у площадок значит «нет данных»


class ShopAdapter(Adapter):
    """Интернет-магазин: Shopify, WooCommerce или страницы с разметкой товаров."""

    def fetch(self):
        base = self.config["url"].rstrip("/")
        errors = []
        for method in (self._shopify, self._woocommerce, self._jsonld):
            try:
                offers = method(base)
            except fetcher.NetworkError:
                raise
            except fetcher.FetchError as error:
                errors.append(str(error))
                continue
            if offers:
                return offers
        raise fetcher.FetchError("не удалось прочитать товары магазина (" + "; ".join(errors[-2:]) + ")")

    def _shopify(self, base):
        offers = []
        for page in range(1, MAX_PAGES + 1):
            data = get_json(f"{base}/products.json?limit=250&page={page}")
            products = data.get("products") if isinstance(data, dict) else None
            if not products:
                break
            for product in products:
                variants = product.get("variants") or []
                prices = [p for p in (_price(v.get("price")) for v in variants) if p]
                quantities = [v.get("inventory_quantity") for v in variants if isinstance(v.get("inventory_quantity"), int)]
                offers.append({
                    "name": product.get("title") or "",
                    "price": min(prices) if prices else None,
                    "stock": sum(max(0, q) for q in quantities) if quantities else None,
                    "sales_week": None,
                    "available": any(v.get("available") for v in variants) if variants else None,
                    "url": f"{base}/products/{product.get('handle')}" if product.get("handle") else base,
                })
        return offers

    def _woocommerce(self, base):
        offers = []
        for page in range(1, MAX_PAGES + 1):
            data = get_json(f"{base}/wp-json/wc/store/v1/products?per_page=100&page={page}")
            if not isinstance(data, list) or not data:
                break
            for product in data:
                prices = product.get("prices") or {}
                minor = int(prices.get("currency_minor_unit") or 0)
                raw = _price(prices.get("price"))
                offers.append({
                    "name": product.get("name") or "",
                    "price": raw / (10 ** minor) if raw else None,
                    "stock": product.get("low_stock_remaining") if isinstance(product.get("low_stock_remaining"), int) else None,
                    "sales_week": None,
                    "available": bool(product.get("is_in_stock")),
                    "url": product.get("permalink") or base,
                })
        return offers

    def _jsonld(self, base):
        offers = []
        for path in self.config.get("pages") or [""]:
            url = base + path
            page_html = _request(url)
            for block in re.findall(
                r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", page_html, re.S | re.I
            ):
                try:
                    data = json.loads(block.strip())
                except ValueError:
                    continue
                offers.extend(_jsonld_products(data, url))
        return offers


def _jsonld_products(data, page_url):
    found = []
    if isinstance(data, list):
        for entry in data:
            found.extend(_jsonld_products(entry, page_url))
        return found
    if not isinstance(data, dict):
        return found
    kind = data.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    if "Product" in kinds:
        offer = data.get("offers") or {}
        if isinstance(offer, list):
            offer = offer[0] if offer else {}
        price = _price(offer.get("price") or offer.get("lowPrice"))
        availability = str(offer.get("availability") or "")
        stock = offer.get("inventoryLevel")
        if isinstance(stock, dict):
            stock = stock.get("value")
        found.append({
            "name": data.get("name") or "",
            "price": price,
            "stock": int(stock) if isinstance(stock, (int, float)) else None,
            "sales_week": None,
            "available": None if not availability else "InStock" in availability,
            "url": data.get("url") or offer.get("url") or page_url,
        })
    for key in ("itemListElement", "@graph", "item"):
        if key in data:
            found.extend(_jsonld_products(data[key], page_url))
    return found


class StarPetsAdapter(Adapter):
    """StarPets: открытый JSON API mm2-market.apineural.com.

    items/all — самая низкая цена каждого предмета (price=null — нет в продаже);
    products/{id}/info — продажи за неделю (numberOfSalesPerWeek). Запросов к API
    мало: ~15 на обход каталога и несколько запросов продаж, по очереди для
    самых дорогих предметов (каждый — не чаще раза в 6 часов).
    """

    API = "https://mm2-market.apineural.com"
    TYPES = ("weapon", "pet", "misc")
    PAGE = 72
    HEADERS = {"Origin": "https://starpets.gg", "Referer": "https://starpets.gg/"}
    INFO_PER_POLL = 4
    INFO_MAX_AGE = 6 * 60 * 60
    PAUSE = 0.4

    def fetch(self):
        api = self.config.get("api", self.API).rstrip("/")
        offers = []
        for kind in self.TYPES:
            group = []
            count = 0
            for page in range(1, MAX_PAGES + 1):
                body = {"currency": "usd", "page": page, "amount": self.PAGE,
                        "filter": {"types": [{"type": kind}]}, "sort": {"popularity": "desc"}}
                data = post_json(f"{api}/api/v2/store/items/all", body, headers=self.HEADERS)
                rows = data.get("items") if isinstance(data, dict) else None
                if not isinstance(rows, list):
                    raise fetcher.FetchError("StarPets ответил в незнакомом формате")
                group.extend(self._offer(row, kind) for row in rows if isinstance(row, dict))
                count = data.get("count") if isinstance(data.get("count"), int) else 0
                if not rows or page * self.PAGE >= count:
                    break
                _pause(self.PAUSE)
            # Список отсортирован по популярности на StarPets: место в нём — тоже сигнал.
            total = max(len(group), count, 1)
            for rank, offer in enumerate(group):
                offer["popularity"] = rank / total
            offers.extend(group)
        self._add_sales(api, offers)
        return offers

    def _offer(self, row, kind):
        name = str(row.get("name") or "")
        if row.get("chroma") and not name.lower().startswith("chroma"):
            name = "Chroma " + name
        price = _price(row.get("price"))
        product_id = row.get("id")
        slug = re.sub(r"[^a-z0-9]+", "-", str(row.get("name") or "").lower().replace("'", "")).strip("-")
        url = f"https://starpets.gg/mm2/shop/{kind}/{slug}/{product_id}" if product_id else self.config["url"]
        subtype = str(row.get("subtype") or "").lower()
        image = row.get("imageUri")
        return {
            "name": name,
            "image": image if isinstance(image, str) and image.startswith("https://") else None,
            "kind": "pet" if kind == "pet" else subtype,
            "price": price,
            "currency": "USD",
            "stock": None,
            "sales_week": None,
            "available": price is not None,
            "url": url,
            "id": product_id,
        }

    def _add_sales(self, api, offers):
        sales = self.cache.setdefault("sales", {})
        now = time.time()
        if self.cache.get("info_off_until", 0) <= now:
            # По очереди: сначала ни разу не запрошенные, потом самые старые;
            # при равенстве — более дорогие.
            due = sorted(
                (o for o in offers if o["id"] is not None and o["price"] is not None
                 and now - sales.get(str(o["id"]), {}).get("at", 0) > self.INFO_MAX_AGE),
                key=lambda o: (sales.get(str(o["id"]), {}).get("at", 0), -(o["price"] or 0)),
            )[:self.INFO_PER_POLL]
            for offer in due:
                _pause(self.PAUSE)
                key = str(offer["id"])
                try:
                    data = get_json(f"{api}/api/v2/products/{offer['id']}/info", headers=self.HEADERS)
                except (fetcher.BlockedError, fetcher.NetworkError):
                    raise
                except fetcher.FetchError:
                    old = sales.get(key, {})
                    sales[key] = {"at": now, "value": old.get("value")}  # в конец очереди
                    self.cache["info_errors"] = self.cache.get("info_errors", 0) + 1
                    if self.cache["info_errors"] >= 3:  # три ошибки подряд — пауза на час
                        self.cache["info_off_until"] = now + 3600
                        self.cache["info_errors"] = 0
                        break
                    continue
                self.cache["info_errors"] = 0
                product = data.get("product") if isinstance(data, dict) else None
                value = product.get("numberOfSalesPerWeek") if isinstance(product, dict) else None
                sales[key] = {"at": now, "value": value if isinstance(value, int) else None}
        if self.cache.get("info_off_until", 0) > now:
            self.note = "продажи за неделю StarPets сейчас не отдаёт — оценка по популярности, повтор через час"
        else:
            known = sum(1 for entry in sales.values() if entry.get("value") is not None)
            self.note = f"продажи за неделю известны для {known} предметов (обновляются по очереди)"
        for offer in offers:
            entry = sales.get(str(offer["id"]))
            if entry and entry.get("value") is not None:
                offer["sales_week"] = entry["value"]


class DreamPetsAdapter(Adapter):
    """DreamPets (dreampets.gg): у площадки нет открытого API.

    Страница рынка даёт список товаров; страницы товаров — минимальную цену
    («от 328.98 ₽») и число лотов в продаже («674 лотов»). Страницы товаров
    читаются по очереди, понемногу за каждый опрос, начиная с самых дорогих.
    """

    PAGES_PER_POLL = 12
    PAUSE = 0.5
    PAGE_TIMEOUT = 15
    _LINK_RE = re.compile(r"""href=["']((?:https?://[^"']*dreampets\.(?:gg|io))?/mm2/product/[^"'#?]+)["']""", re.I)
    _PRICE_RE = re.compile(r"от\s*([\d\s\u00a0.,]+?)\s*₽")
    # "674 лотов", "1 234 лота"; число не склеивается с соседними словами ("ММ2 114 лотов").
    _LOTS_RE = re.compile(r"(?<![\w.,])(\d{1,3}(?:[ \u00a0]\d{3})+|\d+)[ \u00a0]*лот", re.I)

    def fetch(self):
        base = self.config["url"]
        root = re.match(r"https?://[^/]+", base).group(0)
        products = self.cache.setdefault("products", {})  # ссылка -> данные товара
        links = self._catalog(base, root)
        for link in links:
            products.setdefault(link, {"at": 0})
        if not products:
            raise fetcher.FetchError("на странице рынка не нашлось ни одного товара")
        due = sorted(products, key=lambda link: (products[link].get("at", 0), -(products[link].get("price") or 0)))
        read = failed = 0
        last_error = None
        for link in due[:self.PAGES_PER_POLL]:
            _pause(self.PAUSE)
            try:
                page_html = _request(link, headers={"Accept-Language": "ru,en;q=0.8"}, timeout=self.PAGE_TIMEOUT)
            except (fetcher.BlockedError, fetcher.NetworkError):
                raise  # нет связи или блок — опрос считается неудачным
            except fetcher.FetchError as error:
                products[link]["at"] = time.time()  # в конец очереди
                last_error = error
                failed += 1
                if failed >= 2 and not read:
                    break
                continue
            products[link].update(self._parse_product(page_html), at=time.time())
            read += 1
        if failed and not read:
            raise fetcher.FetchError(f"страницы товаров не открываются: {last_error}")
        self.complete = all(entry.get("at") for entry in products.values())
        known = [p for p in products.values() if p.get("name")]
        self.note = f"товаров на рынке: {len(products)}, прочитано: {len(known)} (по {self.PAGES_PER_POLL} за опрос)"
        return [
            {
                "name": entry["name"],
                "price": entry.get("price"),
                "currency": "RUB",
                "stock": entry.get("lots"),
                "sales_week": None,
                "available": bool(entry.get("lots")) if entry.get("lots") is not None else entry.get("price") is not None,
                "url": link,
            }
            for link, entry in products.items() if entry.get("name")
        ]

    def _catalog(self, base, root):
        """Ссылки на товары со страницы рынка (обновляются раз в час)."""
        if time.time() - self.cache.get("catalog_at", 0) < 3600 and self.cache.get("products"):
            return []
        try:
            page_html = _request(base, headers={"Accept-Language": "ru,en;q=0.8"})
            links = self._links(page_html, root)
            if not links:  # рынок рисуется скриптом — откроем как браузер
                browser = fetcher.find_system_browser()
                if browser:
                    links = self._links(fetcher.SystemBrowserFetcher(browser).fetch(base), root)
            if not links:
                links = self._links(_request(root + "/sitemap.xml"), root)
        except (fetcher.BlockedError, fetcher.NetworkError):
            raise
        except fetcher.FetchError:
            links = []
        if links:
            self.cache["catalog_at"] = time.time()
        return links

    def _links(self, page_html, root):
        found = []
        for href in self._LINK_RE.findall(page_html) + re.findall(r"<loc>\s*([^<]*/mm2/product/[^<]+?)\s*</loc>", page_html):
            link = href if href.startswith("http") else root + href
            link = re.sub(r"https?://[^/]+", root, link)  # одно зеркало (.gg), без двойного счёта
            if link not in found:
                found.append(link)
        return found

    def _parse_product(self, page_html):
        title = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.S | re.I)
        name = html_unescape(title.group(1)) if title else ""
        name = re.split(r"\s+(?:/|—|-)\s+", name.strip(), maxsplit=1)[0].strip()
        # Описание страницы ("от 328.98 ₽, 674 лотов"), затем видимый текст — по строкам,
        # чтобы числа из разных мест не склеивались.
        texts = [
            html_unescape(content) for content in re.findall(
                r"<meta[^>]+(?:name|property)=[\"'](?:description|og:description)[\"'][^>]*content=[\"']([^\"']*)[\"']",
                page_html, re.I,
            )
        ] + parser.html_to_text(page_html).splitlines()
        price = next((m for m in (self._PRICE_RE.search(t) for t in texts) if m), None)
        lots = next((m for m in (self._LOTS_RE.search(t) for t in texts) if m), None)
        return {
            "name": name,
            "price": _rub(price.group(1)) if price else None,
            "lots": int(re.sub(r"\D", "", lots.group(1))) if lots else None,
        }


class EldoradoAdapter(Adapter):
    """Eldorado.gg: открытый JSON API предложений продавцов (MM2 — gameId 204).

    Весь рынок MM2 — около 300 страниц, поэтому за один опрос читаются
    следующие PAGES_PER_POLL страниц; полный круг — примерно за час. По каждому
    предмету: самая низкая цена, сколько штук продаётся и у скольких продавцов.
    """

    API = "https://www.eldorado.gg/api/v1/item-management/offers"
    PAGES_PER_POLL = 15
    PAGE_SIZE = 50
    PAUSE = 1.5
    STALE = 3 * 60 * 60  # перерыв дольше 3 часов — данные считаются устаревшими

    def fetch(self):
        api = self.config.get("api", self.API)
        offers = self.cache.setdefault("offers", {})  # id предложения -> данные
        page = self.cache.get("page", 1)
        total_pages = self.cache.get("total_pages")
        cycle = self.cache.setdefault("cycle", 1)
        now = time.time()
        if now - self.cache.get("last_fetch", now) > self.STALE:
            # Долгий перерыв (сон компьютера, нет сети): данные устарели целиком —
            # дочитываем круг заново, не считая исчезнувшее покупками.
            self.cache["refilling"] = True
        self.cache["last_fetch"] = now
        for _ in range(self.PAGES_PER_POLL):
            query = (f"?gameId=204&category=CustomItem&offerSortingCriterion=Price&isAscending=true"
                     f"&pageIndex={page}&pageSize={self.PAGE_SIZE}")
            data = get_json(api + query)
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise fetcher.FetchError("Eldorado ответил в незнакомом формате")
            for row in data["results"]:
                parsed = self._offer(row) if isinstance(row, dict) else None
                if parsed:
                    parsed["cycle"] = cycle
                    offers[parsed.pop("id")] = parsed
            total_pages = data.get("totalPages") if isinstance(data.get("totalPages"), int) else total_pages
            page = page + 1 if total_pages and page < total_pages and data["results"] else 1
            if page == 1:
                # Круг пройден: предложения, не встреченные за круг, ушли с рынка.
                for offer_id in [k for k, v in offers.items() if v.get("cycle", 0) < cycle]:
                    del offers[offer_id]
                cycle += 1
                self.cache["cycles_done"] = self.cache.get("cycles_done", 0) + 1
                self.cache["refilling"] = False
                break
            _pause(self.PAUSE)
        self.cache["page"] = page
        self.cache["total_pages"] = total_pages
        self.cache["cycle"] = cycle
        self.complete = self.cache.get("cycles_done", 0) >= 1 and not self.cache.get("refilling")
        items = {}
        for entry in offers.values():
            item = items.setdefault((entry["name"], entry.get("kind", "")), {
                "name": entry["name"], "kind": entry.get("kind", ""), "price": None, "currency": "USD", "stock": 0,
                "sales_week": None, "available": True, "url": self.config["url"], "sellers": set(),
            })
            if entry["price"] and (item["price"] is None or entry["price"] < item["price"]):
                item["price"] = entry["price"]
            item["stock"] += entry["quantity"]
            item["sellers"].add(entry["seller"])
            if entry["sold_30d"] is not None:
                item["sales_week"] = (item["sales_week"] or 0) + round(entry["sold_30d"] * 7 / 30)
        progress = f"{page - 1 if page > 1 else total_pages or 0} из {total_pages or '?'}"
        self.note = f"прочитано страниц рынка: {progress}, предложений: {len(offers)}"
        result = []
        for item in items.values():
            item["sellers"] = len(item["sellers"])
            result.append(item)
        return result

    def _offer(self, row):
        offer = row.get("offer") if isinstance(row.get("offer"), dict) else {}
        values = {
            str(v.get("name")): str(v.get("value") or "")
            for v in offer.get("tradeEnvironmentValues") or [] if isinstance(v, dict)
        }
        kind = values.get("Item type", "")
        name = values.get("Item name", "").strip()
        if not name or kind.lower() in ("", "other"):
            return None  # VIP-серверы, наборы и прочее
        chroma = any(
            isinstance(value, dict) and str(value.get("name")).lower() == "chroma"
            for attribute in offer.get("attributes") or [] if isinstance(attribute, dict)
            for value in attribute.get("values") or []
        )
        if chroma and not name.lower().startswith("chroma"):
            name = "Chroma " + name
        price = offer.get("pricePerUnitInUSD") or {}
        amount = _price(price.get("amount") if isinstance(price, dict) else None)
        if amount is None or amount < 0.01:
            return None  # приманки за $0.00001
        counts = offer.get("orderCounts") if isinstance(offer.get("orderCounts"), dict) else {}
        user = row.get("user") if isinstance(row.get("user"), dict) else {}
        quantity = offer.get("quantity")
        return {
            "id": str(offer.get("id")),
            "name": name,
            "kind": kind.lower(),
            "price": amount,
            "quantity": quantity if isinstance(quantity, int) and quantity > 0 else 1,
            "seller": str(user.get("id") or offer.get("userId") or ""),
            "sold_30d": counts.get("last30Days") if isinstance(counts.get("last30Days"), int) else None,
        }


def html_unescape(text):
    return html_lib.unescape(re.sub(r"\s+", " ", text)).strip()


def _rub(text):
    """"328.98", "34,99", "1 299,50" -> число рублей."""
    cleaned = re.sub(r"[\s\u00a0]", "", text).rstrip(".,")
    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _pause(seconds):
    time.sleep(seconds)


ADAPTERS = {
    "shop": ShopAdapter,
    "starpets": StarPetsAdapter,
    "dreampets": DreamPetsAdapter,
    "eldorado": EldoradoAdapter,
}


def make_adapter(config, cache=None):
    return ADAPTERS[config["kind"]](config, cache)


# --- наблюдение за площадками ------------------------------------------------

def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


class MarketState:
    def __init__(self, config):
        self.config = config
        self.status = "waiting"
        self.message = ""
        self.last_ok_at = None
        self.next_poll_at = _now()
        self.failures = 0
        self.cache = {}        # данные адаптера между опросами (сохраняются на диск)
        self.offers = {}       # id предмета -> последнее предложение
        self.complete = False  # offers — полный снимок площадки (можно считать покупки)
        self.unmatched = 0
        self.sales = {}        # id предмета -> [[время, количество], ...] за сутки

    def interval(self, default):
        return self.config.get("interval") or default

    def public(self, interval):
        return {
            "id": self.config["id"],
            "title": self.config["title"],
            "url": self.config.get("url"),
            "status": self.status,
            "message": self.message,
            "last_ok_at": self.last_ok_at.isoformat() if self.last_ok_at else None,
            "next_poll_at": self.next_poll_at.isoformat() if self.next_poll_at else None,
            "interval": self.interval(interval),
            "offers": len(self.offers) + self.unmatched,
            "matched": len(self.offers),
        }

    def sold_24h(self, key):
        cutoff = (_now() - timedelta(hours=HISTORY_HOURS)).timestamp()
        events = [event for event in self.sales.get(key, []) if event[0] >= cutoff]
        return sum(event[1] for event in events)


def _valid_sales(value):
    if not isinstance(value, dict):
        return {}
    return {
        str(key): [list(e) for e in events if isinstance(e, (list, tuple)) and len(e) == 2
                   and all(isinstance(x, (int, float)) for x in e)]
        for key, events in value.items() if isinstance(events, list)
    }


class MarketMonitor:
    WAIT_FOR_PRICES = 30  # пока цен Supreme нет, площадки сопоставлять не с чем

    def __init__(self, data_path=None, configs=None, interval=POLL_EVERY, adapters=None):
        self.data_path = data_path
        self.interval = interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.version = 0
        configs = configs if configs is not None else load_configs(data_path)
        self._adapters = adapters or {}
        self.markets = [MarketState(config) for config in configs if config.get("enabled", True)]
        self._load_state()

    # --- состояние на диске ---

    def _state_path(self):
        return Path(self.data_path or store.DATA_FILE).with_name("markets-state.json")

    def _load_state(self):
        try:
            saved = json.loads(self._state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(saved, dict):
            return
        for market in self.markets:
            entry = saved.get(market.config["id"])
            if not isinstance(entry, dict):
                continue
            offers = entry.get("offers") if isinstance(entry.get("offers"), dict) else {}
            market.offers = {str(k): v for k, v in offers.items() if isinstance(v, dict)}
            market.sales = _valid_sales(entry.get("sales"))
            market.cache = entry.get("cache") if isinstance(entry.get("cache"), dict) else {}
            market.complete = bool(entry.get("complete")) and bool(market.offers)
            market.unmatched = entry.get("unmatched") if isinstance(entry.get("unmatched"), int) else 0
            try:
                market.last_ok_at = datetime.fromisoformat(entry["last_ok_at"]) if entry.get("last_ok_at") else None
                if market.last_ok_at and market.last_ok_at.tzinfo is None:
                    market.last_ok_at = market.last_ok_at.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                market.last_ok_at = None
            if market.offers:
                market.status = "ok"
                market.message = "данные с прошлого запуска, скоро обновятся"

    def _save_state(self):
        with self._lock:
            data = {
                market.config["id"]: {
                    "offers": market.offers,
                    "sales": market.sales,
                    "cache": market.cache,
                    "complete": market.complete,
                    "unmatched": market.unmatched,
                    "last_ok_at": market.last_ok_at.isoformat() if market.last_ok_at else None,
                }
                for market in self.markets
            }
            text = json.dumps(data, ensure_ascii=False, default=list)
        path = self._state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass

    # --- опрос ---

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def poll_now(self):
        with self._lock:
            for market in self.markets:
                market.next_poll_at = _now()
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            due = None
            with self._lock:
                for market in self.markets:
                    if market.next_poll_at <= _now():
                        due = market
                        break
                waits = [(m.next_poll_at - _now()).total_seconds() for m in self.markets]
            if due is None:
                self._wake.wait(timeout=max(1, min(waits + [30])))
                self._wake.clear()
                continue
            try:
                self.poll(due)
            except Exception as error:  # поток наблюдения не должен останавливаться
                with self._lock:
                    due.status = "error"
                    due.message = f"внутренняя ошибка: {error}"
                    due.next_poll_at = _now() + timedelta(seconds=due.interval(self.interval))
                    self.version += 1

    def _adapter(self, market):
        factory = self._adapters.get(market.config["kind"])
        return factory(market.config, market.cache) if factory else make_adapter(market.config, market.cache)

    def _fail(self, market, error):
        with self._lock:
            market.failures += 1
            market.status = "error"
            market.message = str(error) or type(error).__name__
            backoff = min(MAX_BACKOFF, market.interval(self.interval) * (2 ** min(market.failures - 1, 5)))
            if isinstance(error, fetcher.BlockedError):
                backoff = max(backoff, BLOCKED_PAUSE)  # площадка просит не спешить — не настаиваем
            market.next_poll_at = _now() + timedelta(seconds=backoff)
            self.version += 1

    def poll(self, market):
        """Опросить одну площадку (вызывается из фонового потока или тестов)."""
        try:
            items = store.load(self.data_path)["items"]
        except Exception as error:
            items, load_error = [], error
        else:
            load_error = None
        if not items:
            with self._lock:
                market.status = "waiting"
                market.message = "ждёт цены Supreme Values" if load_error is None else f"нет цен: {load_error}"
                market.next_poll_at = _now() + timedelta(seconds=self.WAIT_FOR_PRICES)
                self.version += 1
            return
        with self._lock:
            market.status = "busy"
        adapter = self._adapter(market)
        try:
            offers = adapter.fetch()
        except Exception as error:
            self._fail(market, error)
            self._save_state()  # кэш адаптера мог пополниться и до ошибки
            return
        index = build_index(items)
        matched = {}
        unmatched = 0
        for offer in offers:
            item = find_item(index, offer)
            if item is None:
                unmatched += 1
                continue
            key = item_id(item)
            previous = matched.get(key)
            if previous is None:
                matched[key] = dict(offer)
            else:  # несколько предложений одного предмета: самое дешёвое, количество суммируется
                prices = [p for p in (previous.get("price"), offer.get("price")) if p]
                previous["price"] = min(prices) if prices else None
                if offer.get("stock") is not None:
                    previous["stock"] = (previous.get("stock") or 0) + offer["stock"]
                if offer.get("sales_week") is not None:
                    previous["sales_week"] = (previous.get("sales_week") or 0) + offer["sales_week"]
                if offer.get("popularity") is not None:
                    previous["popularity"] = min(previous.get("popularity", 1), offer["popularity"])
                previous["available"] = bool(previous.get("available")) or bool(offer.get("available"))
        now = _now()
        cutoff = (now - timedelta(hours=HISTORY_HOURS)).timestamp()
        with self._lock:
            if market.complete and adapter.complete:
                # Оба снимка полные: исчезнувшие предложения — купленные.
                for key, offer in matched.items():
                    old = market.offers.get(key)
                    if old and isinstance(old.get("stock"), int) and isinstance(offer.get("stock"), int):
                        drop = old["stock"] - offer["stock"]
                        if drop > 0:
                            market.sales.setdefault(key, []).append([now.timestamp(), drop])
            market.sales = {
                key: kept for key, kept in (
                    (key, [event for event in events if event[0] >= cutoff]) for key, events in market.sales.items()
                ) if kept
            }
            if adapter.complete:
                market.offers = matched
            else:
                # Площадка прочитана не целиком: обновляем прочитанное, остальное не трогаем.
                market.offers = dict(market.offers, **matched)
            market.complete = adapter.complete
            market.unmatched = unmatched
            market.failures = 0
            market.status = "ok"
            market.message = adapter.note if offers else "площадка не вернула ни одного предложения"
            market.last_ok_at = now
            market.next_poll_at = now + timedelta(seconds=market.interval(self.interval))
            self.version += 1
        self._save_state()

    # --- данные для окна программы ---

    def status_list(self):
        with self._lock:
            return [market.public(self.interval) for market in self.markets]

    def annotate(self, items):
        """Добавить предметам данные площадок и общую ликвидность."""
        with self._lock:
            snapshot = [
                (market.config, dict(market.offers), {k: market.sold_24h(k) for k in market.offers})
                for market in self.markets
            ]
        for item in items:
            key = item_id(item)
            infos = {}
            for config, offers, sold in snapshot:
                offer = offers.get(key)
                if not isinstance(offer, dict):
                    continue
                info = assess_offer(offer, sold.get(key, 0))
                info.update({
                    "price": offer.get("price"),
                    "currency": offer.get("currency") or "USD",
                    "stock": offer.get("stock"),
                    "sales_week": offer.get("sales_week"),
                    "sold_24h": sold.get(key, 0),
                    "available": offer.get("available"),
                    "url": offer.get("url"),
                    "market_title": config["title"],
                })
                infos[config["id"]] = info
            item["market"] = infos
            item["combined"] = combine(item, list(infos.values()))
            if not item.get("image"):  # картинка с площадки, если на сайте её не нашлось
                item["image_market"] = next(
                    (offers.get(key, {}).get("image") for _, offers, _ in snapshot if offers.get(key, {}).get("image")),
                    None,
                )
        return items


MIN_INTERVAL = 30


def load_configs(data_path=None):
    """Список площадок: data/markets.json, если он есть и правильный, иначе встроенный."""
    path = Path(data_path or store.DATA_FILE).with_name("markets.json")
    try:
        configs = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        configs = None
    valid = [
        dict(config) for config in (configs if isinstance(configs, list) else [])
        if isinstance(config, dict) and config.get("id") and config.get("kind") in ADAPTERS and config.get("url")
    ]
    for config in valid:
        config.setdefault("title", config["id"])
        try:
            interval = int(config.get("interval"))
        except (TypeError, ValueError):
            interval = None
        if interval is None or interval < MIN_INTERVAL:
            config.pop("interval", None)  # неверное значение — обычный интервал
        else:
            config["interval"] = interval
    return valid or [dict(config) for config in DEFAULT_MARKETS]
