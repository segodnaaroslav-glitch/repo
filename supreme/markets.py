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
    """{ключ названия: предмет} — секретные и Untradables не сопоставляются."""
    index = {}
    for item in items:
        if item.get("secret") or item.get("category") == "untradables":
            continue
        for key in {parser.name_key(item["name"]), market_key(item["name"])}:
            if key and key not in index:
                index[key] = item
    return index


def item_id(item):
    return f"{item['category']}:{item['name']}"


# --- оценка ликвидности на площадке ------------------------------------------

def assess_offer(offer, sold_24h):
    """Балл 0–100 и причины по данным одной площадки."""
    reasons = []
    score = 0.0
    if offer.get("sales_week") is not None:
        sales = max(0, offer["sales_week"])
        score = min(100.0, 25 * math.log2(1 + sales))
        reasons.append(f"продаж за неделю: {sales}")
    elif offer.get("stock") is not None:
        stock = max(0, offer["stock"])
        score = min(100.0, 20 * math.log2(1 + stock))
        reasons.append(f"предложений: {stock}")
    if sold_24h:
        score = min(100.0, score + min(40, 10 * sold_24h))
        reasons.append(f"куплено за сутки (по проверкам): {sold_24h}")
    if offer.get("available") is False:
        score = min(score, 10)
        reasons.append("нет в наличии")
    score = int(round(score))
    level = (
        liquidity.LIQUID if score >= liquidity.LIQUID_FROM
        else liquidity.MEDIUM if score >= liquidity.MEDIUM_FROM
        else liquidity.ILLIQUID
    )
    return {"score": score, "level": level, "reasons": reasons}


def combine(item, market_infos):
    """Общая ликвидность: среднее Supreme Values и площадок."""
    supreme = item.get("liquidity") or liquidity.assess(item)
    if supreme["level"] in (liquidity.SECRET, liquidity.UNTRADABLE):
        return dict(supreme, sources=1)
    scores = [supreme["score"]] + [info["score"] for info in market_infos]
    score = int(round(sum(scores) / len(scores)))
    level = (
        liquidity.LIQUID if score >= liquidity.LIQUID_FROM
        else liquidity.MEDIUM if score >= liquidity.MEDIUM_FROM
        else liquidity.ILLIQUID
    )
    reasons = [f"Supreme Values: {supreme['score']}"] + [
        f"{info['market_title']}: {info['score']}" for info in market_infos
    ]
    return {"score": score, "level": level, "reasons": reasons, "sources": len(scores)}


# --- адаптеры площадок --------------------------------------------------------

class Adapter:
    def __init__(self, config, cache=None):
        self.config = config
        self.cache = cache if cache is not None else {}  # живёт между опросами площадки
        self.note = ""  # пояснение для окна программы

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
            for page in range(1, MAX_PAGES + 1):
                body = {"currency": "usd", "page": page, "amount": self.PAGE,
                        "filter": {"types": [{"type": kind}]}, "sort": {"popularity": "desc"}}
                data = post_json(f"{api}/api/v2/store/items/all", body, headers=self.HEADERS)
                rows = data.get("items") if isinstance(data, dict) else None
                if not isinstance(rows, list):
                    raise fetcher.FetchError("StarPets ответил в незнакомом формате")
                offers.extend(self._offer(row, kind) for row in rows if isinstance(row, dict))
                count = data.get("count") if isinstance(data.get("count"), int) else 0
                if not rows or page * self.PAGE >= count:
                    break
                _pause(self.PAUSE)
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
        return {
            "name": name,
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
        if not self.cache.get("info_off"):
            now = time.time()
            due = sorted(
                (o for o in offers if o["id"] is not None and o["price"] is not None
                 and now - sales.get(str(o["id"]), {}).get("at", 0) > self.INFO_MAX_AGE),
                key=lambda o: -(o["price"] or 0),
            )[:self.INFO_PER_POLL]
            for offer in due:
                _pause(self.PAUSE)
                try:
                    data = get_json(f"{api}/api/v2/products/{offer['id']}/info", headers=self.HEADERS)
                except fetcher.BlockedError:
                    raise
                except fetcher.FetchError:
                    self.cache["info_errors"] = self.cache.get("info_errors", 0) + 1
                    if self.cache["info_errors"] >= 3:
                        self.cache["info_off"] = True
                    break
                product = data.get("product") if isinstance(data, dict) else None
                value = product.get("numberOfSalesPerWeek") if isinstance(product, dict) else None
                sales[str(offer["id"])] = {"at": now, "value": value if isinstance(value, int) else None}
        if self.cache.get("info_off"):
            self.note = "продажи за неделю StarPets не отдаёт — оценка по наличию"
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
    _LINK_RE = re.compile(r"""href=["']((?:https?://[^"']*dreampets\.(?:gg|io))?/mm2/product/[^"'#?]+)["']""", re.I)
    _PRICE_RE = re.compile(r"от\s*([\d\s\u00a0.,]+?)\s*₽")
    _LOTS_RE = re.compile(r"(\d[\d\s\u00a0]*)\s*лот", re.I)

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
        read = 0
        for link in due[:self.PAGES_PER_POLL]:
            _pause(self.PAUSE)
            try:
                page_html = _request(link, headers={"Accept-Language": "ru,en;q=0.8"})
            except fetcher.BlockedError:
                raise
            except fetcher.FetchError:
                products[link]["at"] = time.time()
                continue
            products[link].update(self._parse_product(page_html), at=time.time())
            read += 1
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
        except fetcher.BlockedError:
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
        description = " ".join(re.findall(r"<meta[^>]+content=[\"']([^\"']*)[\"']", page_html, re.I))
        text = html_unescape(description) + " " + parser.html_to_text(page_html)
        price = self._PRICE_RE.search(text)
        lots = self._LOTS_RE.search(text)
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
    STALE = 3 * 60 * 60  # предложение, не встреченное за 3 часа, считается ушедшим

    def fetch(self):
        api = self.config.get("api", self.API)
        offers = self.cache.setdefault("offers", {})  # id предложения -> данные
        page = self.cache.get("page", 1)
        total_pages = self.cache.get("total_pages")
        for _ in range(self.PAGES_PER_POLL):
            query = (f"?gameId=204&category=CustomItem&offerSortingCriterion=Price&isAscending=true"
                     f"&pageIndex={page}&pageSize={self.PAGE_SIZE}")
            data = get_json(api + query)
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise fetcher.FetchError("Eldorado ответил в незнакомом формате")
            now = time.time()
            for row in data["results"]:
                parsed = self._offer(row) if isinstance(row, dict) else None
                if parsed:
                    parsed["seen"] = now
                    offers[parsed.pop("id")] = parsed
            total_pages = data.get("totalPages") if isinstance(data.get("totalPages"), int) else total_pages
            page = page + 1 if total_pages and page < total_pages and data["results"] else 1
            if page == 1:
                break  # круг пройден
            _pause(self.PAUSE)
        self.cache["page"] = page
        self.cache["total_pages"] = total_pages
        cutoff = time.time() - self.STALE
        for offer_id in [k for k, v in offers.items() if v["seen"] < cutoff]:
            del offers[offer_id]
        items = {}
        for entry in offers.values():
            item = items.setdefault(entry["name"], {
                "name": entry["name"], "price": None, "currency": "USD", "stock": 0, "sales_week": None,
                "available": True, "url": self.config["url"], "sellers": set(),
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
        self.cache = {}       # данные адаптера между опросами
        self.offers = {}      # id предмета -> последнее предложение
        self.unmatched = 0
        self.sales = {}       # id предмета -> [[время, количество], ...] за сутки

    def interval(self, default):
        return int(self.config.get("interval") or default)

    def public(self, interval):
        interval = self.interval(interval)
        return {
            "id": self.config["id"],
            "title": self.config["title"],
            "url": self.config.get("url"),
            "status": self.status,
            "message": self.message,
            "last_ok_at": self.last_ok_at.isoformat() if self.last_ok_at else None,
            "next_poll_at": self.next_poll_at.isoformat() if self.next_poll_at else None,
            "interval": interval,
            "offers": len(self.offers) + self.unmatched,
            "matched": len(self.offers),
        }

    def sold_24h(self, key):
        cutoff = (_now() - timedelta(hours=HISTORY_HOURS)).timestamp()
        events = [event for event in self.sales.get(key, []) if event[0] >= cutoff]
        return sum(event[1] for event in events)


class MarketMonitor:
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
        for market in self.markets:
            entry = saved.get(market.config["id"]) if isinstance(saved, dict) else None
            if not isinstance(entry, dict):
                continue
            market.offers = entry.get("offers") if isinstance(entry.get("offers"), dict) else {}
            market.sales = entry.get("sales") if isinstance(entry.get("sales"), dict) else {}
            market.unmatched = entry.get("unmatched") if isinstance(entry.get("unmatched"), int) else 0
            try:
                market.last_ok_at = datetime.fromisoformat(entry["last_ok_at"]) if entry.get("last_ok_at") else None
            except ValueError:
                market.last_ok_at = None
            if market.offers:
                market.status = "ok"
                market.message = "данные с прошлого запуска, скоро обновятся"

    def _save_state(self):
        data = {
            market.config["id"]: {
                "offers": market.offers,
                "sales": market.sales,
                "unmatched": market.unmatched,
                "last_ok_at": market.last_ok_at.isoformat() if market.last_ok_at else None,
            }
            for market in self.markets
        }
        path = self._state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
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
            self.poll(due)

    def _adapter(self, market):
        factory = self._adapters.get(market.config["kind"])
        return factory(market.config, market.cache) if factory else make_adapter(market.config, market.cache)

    def poll(self, market):
        """Опросить одну площадку (вызывается из фонового потока или тестов)."""
        with self._lock:
            market.status = "busy"
        adapter = self._adapter(market)
        try:
            offers = adapter.fetch()
            items = store.load(self.data_path)["items"]
        except Exception as error:
            with self._lock:
                market.failures += 1
                market.status = "error"
                market.message = str(error) or type(error).__name__
                backoff = min(MAX_BACKOFF, market.interval(self.interval) * (2 ** min(market.failures - 1, 5)))
                if isinstance(error, fetcher.BlockedError):
                    backoff = max(backoff, BLOCKED_PAUSE)  # площадка просит не спешить — не настаиваем
                market.next_poll_at = _now() + timedelta(seconds=backoff)
                self.version += 1
            return
        index = build_index(items)
        matched = {}
        unmatched = 0
        for offer in offers:
            item = index.get(market_key(offer.get("name", "")))
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
                previous["available"] = bool(previous.get("available")) or bool(offer.get("available"))
        now = _now()
        cutoff = (now - timedelta(hours=HISTORY_HOURS)).timestamp()
        with self._lock:
            for key, offer in matched.items():
                old = market.offers.get(key)
                if old and isinstance(old.get("stock"), int) and isinstance(offer.get("stock"), int):
                    drop = old["stock"] - offer["stock"]
                    if drop > 0:  # предложения исчезли — их купили
                        market.sales.setdefault(key, []).append([now.timestamp(), drop])
            market.sales = {
                key: [event for event in events if event[0] >= cutoff]
                for key, events in market.sales.items()
            }
            market.sales = {key: events for key, events in market.sales.items() if events}
            market.offers = matched
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
                if not offer:
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
        return items


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
    return valid or [dict(config) for config in DEFAULT_MARKETS]
