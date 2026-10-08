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

from . import cdp, dreampets, fetcher, liquidity, parser, store

POLL_EVERY = 120                 # секунд между опросами одной площадки
MAX_BACKOFF = 30 * 60            # при ошибках — реже, но не реже раза в 30 минут
BLOCKED_PAUSE = 15 * 60          # после ответа «слишком много запросов» / «доступ запрещён»
HISTORY_HOURS = 48  # анализ продаж — за последние двое суток
MAX_PAGES = 40

# Площадки по умолчанию. kind: "starpets" — API StarPets; "shop" — интернет-магазин
# (сначала Shopify /products.json, затем WooCommerce Store API, затем разметка
# товаров JSON-LD на страницах из "pages").
DEFAULT_MARKETS = [
    # StarPets: данные обновляются у них раз в ~4 минуты, а после ~50 запросов подряд
    # API блокирует адрес на 10 минут — поэтому опрос раз в 5 минут.
    {"id": "starpets", "title": "StarPets", "kind": "starpets", "url": "https://starpets.gg/mm2", "interval": 300,
     "fee": 0.20},
    {"id": "dreampets", "title": "DreamPets", "kind": "dreampets", "url": "https://dreampets.gg/mm2/", "interval": 120,
     "fee": 0.10},
]
# Eldorado и магазины (kind "eldorado" / "shop") можно добавить своим markets.json.


# --- HTTP ---------------------------------------------------------------------

HTML_HEADERS = {"Accept": fetcher.HEADERS["Accept"], "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"}


def _request(url, data=None, headers=None, timeout=30, raw=False):
    all_headers = dict(fetcher.HEADERS)
    all_headers["Accept"] = "application/json, text/html;q=0.9, */*;q=0.8"
    if data is not None:
        all_headers["Content-Type"] = "application/json"
    all_headers.update(headers or {})
    body = json.dumps(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(url, data=body, headers=all_headers, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body_bytes = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as error:
        if error.code in fetcher.BLOCK_STATUSES:
            raise fetcher.BlockedError(f"площадка не пускает программу (HTTP {error.code})") from error
        raise fetcher.FetchError(f"HTTP {error.code}: {url}") from error
    except (urllib.error.URLError, OSError) as error:
        raise fetcher.NetworkError(f"нет соединения ({error}): {url}") from error
    return body_bytes if raw else body_bytes.decode(charset, "replace")


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


_YEAR_RE = re.compile(r"(?<!\d)(20[0-4]\d)(?!\d)")


def _item_years(item):
    text = " ".join(str(item.get(field) or "") for field in ("name", "origin", "aliases"))
    return {int(year) for year in _YEAR_RE.findall(text)}


def _variant_rank(item, offer):
    """Насколько товар площадки подходит предмету (меньше — лучше): совпадение года
    из названия/происхождения, точное название, есть ли цена, популярность."""
    years = _item_years(item)
    year = offer.get("year")
    year_miss = 0 if not years else (0 if year in years else 1)
    exact = 0 if parser.name_key(offer.get("name") or "") == parser.name_key(item["name"]) else 1
    priced = 0 if offer.get("price") is not None or offer.get("price_rub") is not None else 1
    popularity = offer.get("popularity")
    return (year_miss, priced, exact, popularity if isinstance(popularity, (int, float)) else 1.0)


# --- оценка ликвидности на площадке ------------------------------------------

NO_DATA = "none"


def _level(score):
    if score >= liquidity.LIQUID_FROM:
        return liquidity.LIQUID
    return liquidity.MEDIUM if score >= liquidity.MEDIUM_FROM else liquidity.ILLIQUID


def assess_offer(offer, sold, listed=None):
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
    if sold:
        score = min(100.0, (score or 0) + min(40, 5 * sold))
        reasons.append(f"куплено за 2 дня (по проверкам): {sold}")
    if listed:
        reasons.append(f"новых лотов за 2 дня: {listed}")
        surplus = listed - (sold or 0)
        if score is not None and surplus > 0:
            # Выставляют заметно больше, чем покупают, — продать будет труднее.
            score = max(0.0, score - min(15, 2 * surplus))
    if score is None:
        return {"score": None, "level": NO_DATA, "reasons": ["есть в продаже, но площадка не показывает продажи"]}
    score = int(round(score))
    return {"score": score, "level": _level(score), "reasons": reasons}


def combine(item, market_infos):
    """Общая ликвидность — среднее по площадкам, где у предмета есть данные."""
    supreme = item.get("liquidity") or liquidity.assess(item)
    if supreme["level"] in (liquidity.SECRET, liquidity.UNTRADABLE):
        return dict(supreme, sources=0)
    scored = [info for info in market_infos if info.get("score") is not None]
    reasons = [
        f"{info['market_title']}: {info['score'] if info.get('score') is not None else info['reasons'][0]}"
        for info in market_infos
    ]
    if not scored:
        return {"score": None, "level": NO_DATA, "reasons": reasons or ["на площадках не найден"], "sources": 0}
    score = int(round(sum(info["score"] for info in scored) / len(scored)))
    return {"score": score, "level": _level(score), "reasons": reasons, "sources": len(scored)}


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

    items/all — самая низкая цена каждого товара (price=null — нет в продаже) в
    валюте запроса. Цены в рублях StarPets задаёт сам (это не доллары по курсу),
    поэтому каталог читается и в долларах, и в рублях — по очереди, чтобы не
    превысить лимит запросов (~50, иначе блокировка). products/{id}/info —
    продажи за неделю (numberOfSalesPerWeek), по нескольку предметов за опрос.
    Один товар StarPets — один вариант (год, тип, хрома): разные товары с одним
    названием не смешиваются.
    """

    API = "https://mm2-market.apineural.com"
    TYPES = ("weapon", "pet", "misc")
    CURRENCIES = ("usd", "rub")
    PAGE = 72
    HEADERS = {"Origin": "https://starpets.gg", "Referer": "https://starpets.gg/"}
    INFO_PER_POLL = 4  # каталог (~15 запросов) + продажи — с запасом до блокировки (~50)
    INFO_MAX_AGE = 6 * 60 * 60
    PAUSE = 0.4

    def fetch(self):
        api = self.config.get("api", self.API).rstrip("/")
        prices = self.cache.setdefault("prices", {})  # валюта -> {"at", "rows": {id: цена}}
        # Каждый опрос — одна валюта (та, что читалась давнее); в первый раз — обе.
        due = [c for c in self.CURRENCIES if "at" not in prices.get(c, {})]
        if not due:
            due = [min(self.CURRENCIES, key=lambda c: prices[c]["at"])]
        rows_by_id, complete, notes = {}, True, []
        blocked_seen = False
        for currency in due:
            try:
                rows, whole = self._sweep(api, currency)
            except fetcher.FetchError as error:
                blocked = isinstance(error, (fetcher.BlockedError, fetcher.NetworkError))
                if currency == "usd" or (blocked and not rows_by_id):
                    raise
                # Рубли не получены: в следующий раз — доллары, рубли — последние известные
                # (помечаются «≈», если устарели). at — когда пробовали, ok_at — когда получили.
                prices.setdefault(currency, {"rows": {}})["at"] = time.time()
                blocked_seen = blocked_seen or blocked
                notes.append(f"цены в рублях сейчас не получены ({error})")
                continue
            now = time.time()
            prices[currency] = {"at": now, "ok_at": now,
                                "rows": {key: row.get("price") for key, row in rows.items()}}
            complete = complete and whole
            for key, row in rows.items():
                rows_by_id.setdefault(key, row)
        if not rows_by_id:
            # Каталог этой валюты не прочитан — товары берём из прошлого обхода.
            rows_by_id = self.cache.get("rows") or {}
            complete = False
        else:
            self.cache["rows"] = {key: {k: row.get(k) for k in self._ROW_FIELDS} for key, row in rows_by_id.items()}
        self.complete = complete
        offers = self._offers(rows_by_id, prices)
        if self._rub_stale(prices) and not notes:
            notes.append("цены в рублях StarPets сейчас не получены — показаны последние известные (≈)")
        try:
            # После блокировки в этом опросе продажи не запрашиваем: площадка ещё не пускает.
            self._add_sales(api, offers, request=not blocked_seen)
        except (fetcher.BlockedError, fetcher.NetworkError) as error:
            if not rows_by_id:
                raise
            self.cache["info_off_until"] = time.time() + BLOCKED_PAUSE
            notes.append(f"продажи за неделю сейчас не получены ({error})")
        if notes:
            self.note = "; ".join([self.note] + notes) if self.note else "; ".join(notes)
        return offers

    _ROW_FIELDS = ("id", "name", "type", "subtype", "year", "chroma", "realName", "imageUri", "_rank", "_type")

    def _sweep(self, api, currency):
        """Весь каталог в одной валюте: {id: строка}, прочитан ли он целиком."""
        rows, whole = {}, True
        for kind in self.TYPES:
            group, count = [], 0
            for page in range(1, MAX_PAGES + 1):
                body = {"currency": currency, "page": page, "amount": self.PAGE,
                        "filter": {"types": [{"type": kind}]}, "sort": {"popularity": "desc"}}
                data = post_json(f"{api}/api/v2/store/items/all", body, headers=self.HEADERS)
                items = data.get("items") if isinstance(data, dict) else None
                if not isinstance(items, list):
                    raise fetcher.FetchError("StarPets ответил в незнакомом формате")
                group.extend(row for row in items if isinstance(row, dict) and row.get("id") is not None)
                count = data.get("count") if isinstance(data.get("count"), int) else 0
                if not items or page * self.PAGE >= count:
                    break
                _pause(self.PAUSE)
            seen = {}
            for row in group:  # список мог сдвинуться между страницами — без повторов
                seen.setdefault(str(row["id"]), row)
            if count and len(seen) < count:
                whole = False
            # Список отсортирован по популярности на StarPets: место в нём — тоже сигнал.
            total = max(len(seen), count, 1)
            for rank, (key, row) in enumerate(seen.items()):
                rows.setdefault(key, dict(row, _rank=rank / total, _type=kind))
        return rows, whole

    def _rub_stale(self, prices):
        """Рубли давно не обновлялись (площадка их не отдаёт) — показывать как примерные."""
        entry = prices.get("rub", {})
        ok_at = entry.get("ok_at", entry.get("at", 0))  # кэш прошлой версии хранил только at
        return bool(entry.get("rows")) and time.time() - ok_at > 3 * (self.config.get("interval") or 300)

    def _offers(self, rows, prices):
        usd = prices.get("usd", {}).get("rows", {})
        rub = prices.get("rub", {}).get("rows", {})
        rub_stale = self._rub_stale(prices)
        # Есть ли товар в продаже — по более свежему из двух снимков.
        usd_newer = prices.get("usd", {}).get("ok_at", 0) >= prices.get("rub", {}).get("ok_at", 0)
        # Курс самой площадки (медиана рубли/доллары) — только для товаров, у которых
        # рублёвой цены ещё нет; такие цены помечаются как примерные.
        ratios = sorted(_price(rub[k]) / _price(usd[k]) for k in usd
                        if _price(usd.get(k)) and _price(rub.get(k)) and _price(usd[k]) >= 1)
        site_rate = ratios[len(ratios) // 2] if ratios else None
        offers = []
        for key, row in rows.items():
            price = _price(usd.get(key))
            price_rub = _price(rub.get(key))
            approx = rub_stale and price_rub is not None
            if price_rub is None and price is not None and site_rate and (key not in rub or rub_stale):
                price_rub, approx = round(price * site_rate, 2), True
            if usd_newer or approx or key not in rub:
                available = price is not None
            else:
                available = _price(rub.get(key)) is not None
            offers.append(self._offer(row, price, price_rub, approx, available))
        return offers

    def _offer(self, row, price, price_rub=None, rub_approx=False, available=None):
        kind = row.get("_type") or row.get("type") or "weapon"
        name = str(row.get("name") or "")
        if row.get("chroma") and not name.lower().startswith("chroma"):
            name = "Chroma " + name
        product_id = row.get("id")
        slug = re.sub(r"[^a-z0-9]+", "-", str(row.get("name") or "").lower().replace("'", "")).strip("-")
        url = f"https://starpets.gg/mm2/shop/{kind}/{slug}/{product_id}" if product_id else self.config["url"]
        subtype = str(row.get("subtype") or "").lower()
        image = row.get("imageUri")
        year = row.get("year")
        try:
            year = int(year) if year is not None else None
        except (TypeError, ValueError):
            year = None
        return {
            "name": name,
            "image": image if isinstance(image, str) and image.startswith("https://") else None,
            "kind": "pet" if kind == "pet" else subtype,
            "year": year,
            "real_name": row.get("realName"),
            "price": price,
            "price_rub": price_rub,
            "price_rub_approx": rub_approx,
            "currency": "USD",
            "stock": None,
            "sales_week": None,
            "available": (price is not None or (price_rub is not None and not rub_approx))
                         if available is None else available,
            "popularity": row.get("_rank"),
            "url": url,
            "id": product_id,
        }

    def _add_sales(self, api, offers, request=True):
        sales = self.cache.setdefault("sales", {})
        now = time.time()
        if request and self.cache.get("info_off_until", 0) <= now:
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
    """DreamPets (dreampets.gg): открытого API нет, поэтому товары и цены ищутся
    всеми способами, а не одним:

    1. страница рынка: карточки со ссылками, ссылки внутри данных страницы
       (Next.js/Nuxt), встроенные данные с ценами;
    2. адреса данных, которые раньше нашёл браузер (если они открываются напрямую);
    3. карта сайта (robots.txt, sitemap, вложенные и сжатые) — раз в час;
    4. страницы товаров — параллельно, по очереди (сначала давно не читанные):
       «от 328.98 ₽» и «674 лотов» из данных страницы, описания или текста;
       ошибка одной страницы не мешает остальным;
    5. если площадка не пускает программу или рисует всё скриптом — скрытый Edge:
       открывает рынок как обычный посетитель, докручивает список и читает карточки.

    Если цен не нашлось, в папку debug/dreampets кладётся отчёт для проверки.
    """

    PAGES_PER_POLL = 120
    PAGES_TIME = 90  # секунд на страницы товаров за опрос (другие площадки ждут)
    BROWSER_PAGES = 60
    WORKERS = 4
    PAGE_TIMEOUT = 20
    PAUSE = 0.15
    SITEMAP_EVERY = 60 * 60
    BROWSER_EVERY = 10 * 60
    MAX_SITEMAPS = 40
    USELESS_PAGES = 6  # столько страниц подряд без цены — страницы рисуются скриптом
    _LINK_RE = re.compile(r"""href=["']((?:https?://[^"']*dreampets\.(?:gg|io))?/mm2/product/[^"'#?]+)["']""", re.I)
    _CARD_SELECTOR = 'a[href*="/mm2/product/"]'

    def fetch(self):
        base = self.config["url"]
        self.root = re.match(r"https?://[^/]+", base).group(0)
        now = time.time()
        products = self.cache.setdefault("products", {})  # uuid -> данные товара
        self._migrate_cache(products)
        self.report = {"time": store.now_iso(), "base": base, "steps": []}
        self.sources = {}
        self.fresh = set()  # товары, у которых цена и лоты уже получены в этом опросе
        self.fresh_price = set()  # товары с ценой из списка (рынок, данные, браузер) в этом опросе
        blocked = None

        market_html = None
        try:
            market_html = self._market_page(base)
        except fetcher.BlockedError as error:
            blocked = error
        if market_html:
            self._absorb_market(market_html, products, now)

        for url in list(self.cache.get("api_urls") or [])[:5]:
            if blocked:
                break
            try:
                self._absorb_json(get_json(url, headers={"Origin": self.root, "Referer": base}), products, now, "api")
            except fetcher.BlockedError as error:
                blocked = error
            except fetcher.FetchError as error:
                self._step("api", url, str(error))

        if not blocked and (not products or now - self.cache.get("sitemap_at", 0) >= self.SITEMAP_EVERY):
            try:
                self._sitemaps(products)
            except fetcher.BlockedError as error:
                blocked = error

        pages_useless = self.cache.get("pages_useless_until", 0) > now
        priced_now = sum(1 for key in products if key in self.fresh)
        need_browser = blocked or not products or (pages_useless and priced_now < len(products) / 2)
        if need_browser and now - self.cache.get("browser_at", 0) >= self.BROWSER_EVERY:
            self.cache["browser_at"] = now
            try:
                self._browser_pass(base, products, now, read_pages=bool(blocked) and not pages_useless)
            except Exception as error:  # браузер — запасной путь, его сбой не ломает опрос
                self._step("browser", "error", f"{type(error).__name__}: {error}")

        if not blocked and not pages_useless:
            try:
                self._read_pages(products)
            except fetcher.BlockedError as error:
                blocked = error

        offers = self._offers(products)
        priced = sum(1 for offer in offers if offer["price"] is not None)
        self.complete = bool(products) and all(entry.get("at") for entry in products.values())
        parts = [f"товаров: {len(products)}, с ценой: {priced}"]
        parts += [f"{name}: {count}" for name, count in self.sources.items() if count]
        self.note = ", ".join(parts)
        if not priced:
            self._save_report(market_html)
            reason = self._why_empty(blocked, products, market_html)
            if blocked:
                raise fetcher.BlockedError(reason)
            raise fetcher.FetchError(reason)
        if blocked:
            self.note += f" (часть данных не обновлена: {blocked})"
        return offers

    # --- общие части ---

    def _step(self, name, what, result):
        self.report["steps"].append({"step": name, "what": str(what)[:300], "result": str(result)[:500]})

    def _count(self, source, number=1):
        self.sources[source] = self.sources.get(source, 0) + number

    def _migrate_cache(self, products):
        """Кэш прошлой версии хранил товары по ссылке — переводим на uuid."""
        for key in [k for k in products if k.startswith("http") and dreampets.uuid_of(k)]:
            entry = products.pop(key)
            if isinstance(entry, dict):
                entry.setdefault("url", self._absolute(key))
                products.setdefault(dreampets.uuid_of(key), entry)
        for key in [k for k, e in products.items() if not isinstance(e, dict)]:
            products.pop(key)

    def _key(self, link):
        """Ключ товара: uuid из ссылки (одинаковый на .gg и .io), иначе сама ссылка."""
        return dreampets.uuid_of(link) or self._absolute(link)

    def _absolute(self, href):
        link = href if href.startswith("http") else self.root + href
        return re.sub(r"https?://[^/]+", self.root, link)  # одно зеркало, без двойного счёта

    def _upsert(self, products, data, now, source, link=None, key=None):
        """Добавить или обновить товар: по ссылке (uuid), иначе по названию."""
        if key is None and link:
            key = self._key(link)
        if key is None and data.get("id") is not None:
            key = dreampets.uuid_of(str(data["id"]))
        name = dreampets.clean_name(data.get("name")) if data.get("name") else None
        if key is None:
            if not name:
                return None
            wanted = parser.name_key(name)
            key = next((k for k, e in products.items() if parser.name_key(e.get("name") or "") == wanted), None)
            key = key or "name:" + wanted
        entry = products.setdefault(key, {"at": 0})
        if link:
            entry["url"] = self._absolute(link)
        if name and (not entry.get("name") or source != "slug"):
            entry["name"] = name
        elif not entry.get("name") and link:
            entry["name"] = dreampets.slug_name(link)
        if not key.startswith("name:") and entry.get("name"):
            # Тот же товар мог прийти раньше без ссылки (по названию) — сливаем в одну запись.
            twin = "name:" + parser.name_key(entry["name"])
            if twin in products and twin != key:
                old = products.pop(twin)
                if old.get("at", 0) > entry.get("at", 0):  # новые данные ниже всё равно перезапишут
                    for field in ("price", "lots", "at", "source"):
                        if old.get(field) is not None:
                            entry[field] = old[field]
                for marks in (self.fresh, self.fresh_price):
                    if twin in marks:
                        marks.discard(twin)
                        marks.add(key)
        got = False
        if data.get("price") is not None:
            entry["price"] = data["price"]
            got = True
        if data.get("lots") is not None:
            entry["lots"] = data["lots"]
        if got:
            entry["at"] = now
            entry["source"] = source
            if source != "страницы товаров":
                self.fresh_price.add(key)  # цена со списка свежее страницы товара
            if data.get("lots") is not None:
                self.fresh.add(key)
            self._count(source)
        return key

    def _market_page(self, base):
        """Страница рынка (при недоступности — зеркало dreampets.io)."""
        urls = [base]
        mirror = re.sub(r"dreampets\.gg", "dreampets.io", base) if "dreampets.gg" in base else None
        if mirror:
            urls.append(mirror)
        last = None
        for url in urls:
            try:
                page_html = _request(url, headers=HTML_HEADERS, timeout=self.PAGE_TIMEOUT)
                self._step("market", url, f"ok, {len(page_html)} символов")
                return page_html
            except fetcher.BlockedError as error:
                self._step("market", url, str(error))
                raise
            except fetcher.NetworkError as error:
                self._step("market", url, str(error))
                last = error
            except fetcher.FetchError as error:
                self._step("market", url, str(error))
                return None
        raise last

    # --- страница рынка ---

    def _absorb_market(self, page_html, products, now):
        links = dreampets.product_links(page_html, self.root)
        for link in links:
            self._upsert(products, {"name": None}, now, "slug", link)
        cards = self._cards(page_html)
        for link, card in cards.items():
            self._upsert(products, card, now, "страница рынка", link)
        embedded = 0
        for blob in dreampets.embedded_json(page_html):
            embedded += self._absorb_json(blob, products, now, "данные страницы")
        self._step("market-parse", "links/cards/embedded", f"{len(links)}/{len(cards)}/{embedded}")

    def _cards(self, page_html):
        """Карточки со ссылками: текст от ссылки на товар до ссылки на другую страницу."""
        anchors = [(m.start(), m.group(1)) for m in re.finditer(r"""<a\b[^>]*href=["']([^"']+)["']""", page_html, re.I)]
        matches = list(self._LINK_RE.finditer(page_html))
        cards = {}
        for match in matches:
            link = self._absolute(match.group(1))
            end = next((pos for pos, href in anchors if pos > match.end() and self._absolute(href) != link),
                       min(len(page_html), match.end() + 4000))
            end = min(end, match.end() + 4000)
            start = page_html.rfind("<", 0, match.start())
            # Соседние теги карточки — разные надписи: "<i>114</i><b>23,71 ₽</b>" не "11423,71 ₽".
            fragment = re.sub(r">(?=<)", "> ", page_html[max(0, start):end])
            card = dreampets.parse_card_text(parser.html_to_text(fragment))
            if not card["name"]:
                card["name"] = dreampets.slug_name(link)
            known = cards.setdefault(link, {"name": None, "price": None, "lots": None})
            for field in ("name", "price", "lots"):
                if known[field] is None:
                    known[field] = card[field]
        return cards

    def _absorb_json(self, data, products, now, source):
        """Товары с ценами из любых данных (JSON страницы или API). Возвращает их число."""
        found = 0
        for product in dreampets.walk_products(data):
            link = None
            slug = product.get("slug")
            if product.get("id") is not None and dreampets.uuid_of(str(product["id"])):
                uid = dreampets.uuid_of(str(product["id"]))
                link = f"{self.root}/mm2/product/{slug + '/' if slug else ''}{uid}"
            if self._upsert(products, product, now, source, link):
                found += 1
        return found

    # --- карта сайта ---

    def _sitemaps(self, products):
        queue = []
        try:
            queue += dreampets.sitemap_urls_from_robots(_request(self.root + "/robots.txt", headers=HTML_HEADERS,
                                                                 timeout=self.PAGE_TIMEOUT))
        except fetcher.BlockedError:
            raise
        except fetcher.FetchError as error:
            self._step("robots", self.root + "/robots.txt", str(error))
        for default in (self.root + "/sitemap.xml", self.root + "/sitemap_index.xml"):
            if default not in queue:
                queue.append(default)
        seen, found = set(), 0
        while queue and len(seen) < self.MAX_SITEMAPS:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                raw = _request(url, headers=HTML_HEADERS, timeout=self.PAGE_TIMEOUT, raw=True)
            except fetcher.BlockedError:
                raise
            except fetcher.FetchError as error:
                self._step("sitemap", url, str(error))
                continue
            children, pages = dreampets.parse_sitemap(raw)
            queue += [c for c in children if c not in seen]
            for page in pages:
                if dreampets.PRODUCT_PATH_RE.search(page):
                    if self._upsert(products, {"name": None}, 0, "slug", dreampets.PRODUCT_PATH_RE.search(page).group(1)):
                        found += 1
            self._step("sitemap", url, f"страниц: {len(pages)}, вложенных карт: {len(children)}")
        self.cache["sitemap_at"] = time.time()
        if found:
            self._count("карта сайта", found)

    # --- страницы товаров ---

    def _read_pages(self, products):
        from concurrent.futures import ThreadPoolExecutor  # noqa: E402 (нужен только здесь)
        waiting = [key for key, entry in products.items() if key not in self.fresh and entry.get("url")]
        due = sorted(waiting, key=lambda k: (products[k].get("page_at", 0), -(products[k].get("price") or 0)))
        due = due[:self.PAGES_PER_POLL]
        if not due:
            return
        stop = threading.Event()
        blocked = []
        deadline = time.time() + self.PAGES_TIME  # остальные страницы — в следующий опрос

        def read(key):
            if stop.is_set() or time.time() > deadline:
                return key, None, None
            url = products[key]["url"]
            try:
                return key, _request(url, headers=HTML_HEADERS, timeout=self.PAGE_TIMEOUT), None
            except fetcher.BlockedError as error:
                stop.set()  # площадка просит остановиться — остальные страницы в следующий раз
                blocked.append(error)
                return key, None, error
            except fetcher.FetchError as error:
                return key, None, error
            finally:
                _pause(self.PAUSE)

        read_ok = useless = 0
        failures = []
        with ThreadPoolExecutor(max_workers=self.WORKERS) as pool:
            for key, page_html, error in pool.map(read, due):
                entry = products[key]
                if page_html is None and error is None:
                    continue  # не запрашивалась (остановка)
                entry["page_at"] = time.time()  # в конец очереди — и при ошибке
                if error is not None:
                    failures.append(str(error))
                    entry.setdefault("at", 0)
                    continue
                parsed = dreampets.parse_product_page(page_html)
                read_ok += 1
                if parsed["price"] is None and parsed["lots"] is None:
                    useless += 1
                    if not entry.get("at"):
                        entry["at"] = time.time()  # прочитана: на этой странице цены нет
                    continue
                useless = 0
                data = dict(parsed)
                if key in self.fresh_price:
                    data.pop("price")  # цена со страницы рынка свежее
                elif data["price"] is None:
                    data.pop("price")  # продан: цены нет, но лоты известны
                    entry["price"] = None
                    entry["at"] = time.time()
                if key in self.fresh_price and data.get("lots") is not None:
                    entry["lots"] = data["lots"]
                    entry["at"] = time.time()
                    self.fresh.add(key)
                self._upsert(products, data, time.time(), "страницы товаров", key=key)
        self._step("pages", f"{len(due)} в очереди", f"прочитано {read_ok}, ошибок {len(failures)}"
                   + (f": {failures[-1]}" if failures else ""))
        if read_ok >= self.USELESS_PAGES and useless >= self.USELESS_PAGES:
            # Страницы товаров рисуются скриптом: обычным запросом цен не прочитать.
            self.cache["pages_useless_until"] = time.time() + 6 * 3600
        if blocked:
            raise blocked[0]

    # --- скрытый браузер ---

    def _browser_pass(self, base, products, now, read_pages):
        with cdp.Browser() as browser:
            browser.open(base, settle=4)
            result = browser.evaluate(cdp.JS_COLLECT_CARDS % (json.dumps(self._CARD_SELECTOR), 30), timeout=150) or {}
            cards = result.get("cards") or []
            for card in cards:
                data = dreampets.parse_card_text(card.get("text"))
                link = dreampets.PRODUCT_PATH_RE.search(card.get("href") or "")
                if link:
                    self._upsert(products, data, now, "браузер", link.group(1))
            self._step("browser", base, f"карточек: {len(cards)}, заголовок: {result.get('title')}")
            # Запросы, которые сделала сама страница: если в ответе товары с ценами —
            # запоминаем адрес и дальше читаем его напрямую.
            api = [u for u in (result.get("api") or []) if not re.search(r"\.(js|css|png|jpe?g|webp|svg|woff2?)(\?|$)", u)]
            responses = browser.evaluate(cdp.JS_FETCH_MANY % json.dumps(api[:20]), timeout=120) or {} if api else {}
            useful = []
            for url, (status, text) in responses.items():
                try:
                    data = json.loads(text)
                except ValueError:
                    continue
                if self._absorb_json(data, products, now, "данные площадки") >= 10:
                    useful.append(url)
            self.report["api_seen"] = api[:50]
            if useful:
                self.cache["api_urls"] = useful[:5]
            if read_pages:
                # Обычные запросы не пускают — страницы товаров через браузер (с его cookies).
                due = [e["url"] for k, e in sorted(products.items(), key=lambda kv: kv[1].get("page_at", 0))
                       if e.get("url") and k not in self.fresh][:self.BROWSER_PAGES]
                pages = browser.evaluate(cdp.JS_FETCH_MANY % json.dumps(due), timeout=240) or {} if due else {}
                for url, (status, text) in pages.items():
                    key = self._key(url)
                    if status == 200:
                        parsed = dreampets.parse_product_page(text)
                        if parsed["price"] is not None or parsed["lots"] is not None:
                            self._upsert(products, parsed, time.time(), "браузер", key=key)
                    if key in products:
                        products[key]["page_at"] = time.time()

    # --- результат ---

    def _offers(self, products):
        offers = []
        for key, entry in products.items():
            lots = entry.get("lots")
            price = entry.get("price")
            if not entry.get("name") or (price is None and lots is None):
                continue  # ещё не прочитан — данных нет (это не «нет в продаже»)
            offers.append({
                "id": key,
                "name": entry["name"],
                "price": price,
                "currency": "RUB",
                "stock": lots,
                "sales_week": None,
                "available": bool(lots) if lots is not None else price is not None,
                "url": entry.get("url") or self.config["url"],
            })
        return offers

    def _why_empty(self, blocked, products, market_html):
        if blocked:
            return f"DreamPets не пускает программу ({blocked}); отчёт сохранён в папке debug/dreampets"
        if not products:
            if market_html is None:
                return "страница рынка DreamPets не открылась; отчёт сохранён в папке debug/dreampets"
            return ("на странице рынка DreamPets не нашлось товаров (сайт рисует их скриптом, а Edge/Chrome "
                    "не помог); отчёт сохранён в папке debug/dreampets")
        return (f"товаров DreamPets: {len(products)}, но цены пока не прочитаны — страницы читаются по очереди; "
                "если так и останется, пришлите папку debug/dreampets")

    def _save_report(self, market_html):
        try:
            folder = Path(store.DATA_FILE).parent / "debug" / "dreampets"
            folder.mkdir(parents=True, exist_ok=True)
            if market_html:
                (folder / "market.html").write_text(market_html[:3_000_000], encoding="utf-8")
            report = dict(self.report, sources=self.sources, products=len(self.cache.get("products", {})))
            (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass


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
    """"328.98", "34,99", "1 299,50", "1,299.50", "12,345", "1.299,50" -> число рублей."""
    cleaned = re.sub(r"[\s\u00a0]", "", text).rstrip(".,")
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):  # 1.299,50
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:                                        # 1,299.50
            cleaned = cleaned.replace(",", "")
    elif re.fullmatch(r"\d{1,3}(?:[,.]\d{3})+", cleaned):
        cleaned = re.sub(r"[,.]", "", cleaned)       # 12,345 / 1.299 — разделители тысяч
    else:
        cleaned = cleaned.replace(",", ".")
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
        self.sales = {}        # id предмета -> [[время, количество], ...] ушедших лотов за 2 дня
        self.listed = {}       # id предмета -> [[время, количество], ...] новых лотов за 2 дня

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

    @staticmethod
    def _total(events):
        cutoff = (_now() - timedelta(hours=HISTORY_HOURS)).timestamp()
        return sum(event[1] for event in events if event[0] >= cutoff)

    def sold(self, key):
        return self._total(self.sales.get(key, []))

    def new_lots(self, key):
        return self._total(self.listed.get(key, []))


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
            market.listed = _valid_sales(entry.get("listed"))
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
                    "listed": market.listed,
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
        by_key = {item_id(item): item for item in items}
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
            elif (offer.get("id") is not None and previous.get("id") is not None and offer["id"] != previous["id"]
                  and (offer.get("year") is not None or previous.get("year") is not None)):
                # Разные товары площадки с одним названием (Cane 2015 / 2018 / 2021):
                # не смешивать, а выбрать подходящий этому предмету.
                if _variant_rank(by_key[key], offer) < _variant_rank(by_key[key], previous):
                    matched[key] = dict(offer)
            else:  # несколько предложений одного товара: самое дешёвое, количество суммируется
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
            # Прошлый снимок мог быть сделан давно (программа была закрыта, компьютер спал,
            # не было сети): всё, что поменялось за это время, — не «за 2 дня по проверкам».
            fresh = (market.last_ok_at is not None and
                     (now - market.last_ok_at).total_seconds() <= max(3 * market.interval(self.interval), 30 * 60))
            if fresh and market.complete and adapter.complete:
                # Оба снимка полные: исчезнувшие лоты — купленные, появившиеся — новые.
                for key, offer in matched.items():
                    old = market.offers.get(key)
                    if old and isinstance(old.get("stock"), int) and isinstance(offer.get("stock"), int):
                        change = offer["stock"] - old["stock"]
                        if change < 0:
                            market.sales.setdefault(key, []).append([now.timestamp(), -change])
                        elif change > 0:
                            market.listed.setdefault(key, []).append([now.timestamp(), change])
            for events_by_item in ("sales", "listed"):
                setattr(market, events_by_item, {
                    key: kept for key, kept in (
                        (key, [event for event in events if event[0] >= cutoff])
                        for key, events in getattr(market, events_by_item).items()
                    ) if kept
                })
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

    def configs(self):
        return [market.config for market in self.markets]

    def annotate(self, items):
        """Добавить предметам данные площадок и общую ликвидность."""
        with self._lock:
            snapshot = [
                (market.config, dict(market.offers),
                 {k: (market.sold(k), market.new_lots(k)) for k in market.offers})
                for market in self.markets
            ]
        for item in items:
            key = item_id(item)
            infos = {}
            for config, offers, sold in snapshot:
                offer = offers.get(key)
                if not isinstance(offer, dict):
                    continue
                sold_48h, listed_48h = sold.get(key, (0, 0))
                if not isinstance(offer.get("stock"), int):
                    sold_48h = listed_48h = None  # площадка не показывает лоты — купленное не измерить
                info = assess_offer(offer, sold_48h, listed_48h)
                info.update({
                    "price": offer.get("price"),
                    "price_rub": offer.get("price_rub"),
                    "price_rub_approx": bool(offer.get("price_rub_approx")),
                    "year": offer.get("year"),
                    "currency": offer.get("currency") or "USD",
                    "fee": config.get("fee", 0),
                    "stock": offer.get("stock"),
                    "sales_week": offer.get("sales_week"),
                    "sold_48h": sold_48h,
                    "listed_48h": listed_48h,
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
        try:
            fee = float(config.get("fee"))
        except (TypeError, ValueError):
            fee = None
        if fee is None or not 0 <= fee < 1:  # нет комиссии или она неверная — как у площадки по умолчанию
            fee = next((c["fee"] for c in DEFAULT_MARKETS if c["kind"] == config["kind"]), 0)
        config["fee"] = fee
    return valid or [dict(config) for config in DEFAULT_MARKETS]
