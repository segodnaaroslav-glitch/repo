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
    text = parser.chroma_name(name) or str(name)  # «Lightbringer (Chroma)» — это «Chroma Lightbringer»
    text = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", text)  # "(Godly Knife)", "[MM2]"
    text = re.sub(r"^c\.\s*", "chroma ", text.strip(), flags=re.I)
    text = _NOISE_RE.sub(" ", text)
    return parser.name_key(text)


# Редкость товара на площадке -> категория Supreme Values. Один и тот же «Laser» бывает
# Godly, Vintage (2014), Chroma и Rare-ножом 2026 — это разные предметы с разной ценой.
RARITY_CATEGORY = {
    "godly": "godlies", "ancient": "ancients", "unique": "uniques", "vintage": "vintages",
    "legendary": "legendaries", "rare": "rares", "uncommon": "uncommons", "common": "commons",
}
_RARITY_WORDS = (
    ("uncommon", r"uncommon|необычн"), ("common", r"common|обычн"), ("godly", r"godly|годли|божеств"),
    ("ancient", r"ancient|древн|эншент"), ("unique", r"unique|уникальн"),
    ("vintage", r"vintage|винтаж|classic|классич"),  # DreamPets зовёт винтажные «Classic»
    ("legendary", r"legendary|легендарн"), ("rare", r"rare|редк"),
    # праздничная редкость (значок CHRISTMAS / HALLOWEEN на DreamPets) — дешёвые предметы событий
    ("event", r"christmas|halloween|рождеств|новогод|хэллоуин|хеллоуин"),
)
# Дорогие категории: праздничный (CHRISTMAS/HALLOWEEN) товар к ним не подходит — у Godly
# праздничных предметов значок GODLY, а не CHRISTMAS.
_VALUABLE = ("godlies", "ancients", "uniques", "chromas", "vintages", "legendaries", "pets", "sets")
# Предмет с неизвестной редкостью (площадка её не показала) из нескольких одноимённых:
# сначала то, чем торгуют чаще всего.
_CATEGORY_PREFERENCE = ("godlies", "ancients", "chromas", "uniques", "legendaries", "rares", "uncommons",
                        "commons", "vintages", "pets", "misc", "sets")


def category_rank(item):
    """Порядок одноимённых предметов: сначала то, чем чаще торгуют."""
    try:
        return _CATEGORY_PREFERENCE.index(item.get("category"))
    except ValueError:
        return len(_CATEGORY_PREFERENCE)


def rarity(value):
    """'Godly', 'godly', 'Годли', 'Винтажный' -> 'godly' / 'vintage' …; иначе None."""
    text = str(value or "").strip().lower()
    for name, pattern in _RARITY_WORDS:
        if re.match(rf"(?:{pattern})", text):
            return name
    return None


def expected_category(offer):
    """Категория Supreme Values, которой подходит товар площадки, или None (неизвестно)."""
    if str(offer.get("kind") or "").lower() == "pet":
        return "pets"
    if parser.chroma_name(offer.get("name")) or re.match(r"c\.\s", str(offer.get("name") or "").strip(), re.I):
        return "chromas"
    return RARITY_CATEGORY.get(rarity(offer.get("rarity")))


def build_index(items):
    """Поиск предметов Supreme Values по названию товара на площадке.

    Точное название ищется первым. Упрощённый ключ (без "Gun", "Knife", "(Godly)")
    используется, только если он однозначен: "Flowerwood" и "Flowerwood Gun" —
    разные предметы. Секретные и Untradables не сопоставляются. Одноимённые предметы
    разных категорий (Laser — Godlies и Laser — Vintages) хранятся все, выбор — по
    редкости товара (find_item).
    """
    exact, every, loose, ambiguous = {}, {}, {}, set()
    for item in items:
        if item.get("secret") or item.get("category") == "untradables":
            continue
        exact.setdefault(parser.name_key(item["name"]), item)
        every.setdefault(parser.name_key(item["name"]), []).append(item)
        key = market_key(item["name"])
        if not key:
            continue
        if key in loose and loose[key] is not item:
            ambiguous.add(key)
        else:
            loose.setdefault(key, item)
    for key in ambiguous:
        loose.pop(key, None)
    for same in every.values():
        same.sort(key=category_rank)
    for key, same in every.items():
        exact[key] = same[0]
    return {"exact": exact, "every": every, "loose": loose}


def _pick(candidates, offer):
    """Из одноимённых предметов — подходящий товару по редкости. Редкость известна, но
    ни один не подходит (Laser — Rare-нож 2026, а в списке Godly и Vintage) — None:
    чужая цена хуже, чем никакой."""
    if len(candidates) < 2:
        return candidates[0] if candidates else None
    category = expected_category(offer)
    if category is None:
        return candidates[0]
    return next((item for item in candidates if item.get("category") == category), None)


def candidate_items(index, offer):
    """Все предметы Supreme Values с названием товара площадки (одноимённые — все)."""
    name = str(offer.get("name") or "")
    kind = str(offer.get("kind") or "").lower()
    every = index.get("every") or {key: [item] for key, item in index["exact"].items()}
    if kind in ("gun", "knife", "pet"):
        same = every.get(parser.name_key(f"{name} {kind}"))
        if same:
            return list(same)
    same = every.get(parser.name_key(name))
    if same:
        return list(same)
    item = index["loose"].get(market_key(name))
    return [item] if item else []


# Соседние ступени редкости: площадка и Supreme Values иногда относят предмет к соседней
# (Common/Uncommon) — такой товар годится, если лучшего нет. Дальше — это другой предмет.
_LADDER = ("commons", "uncommons", "rares", "legendaries", "godlies", "ancients")
_KIND_WORDS = (("gun", re.compile(r"(?:^|[^a-z])(?:gun|g)(?:[^a-z]|$)")), ("knife", re.compile(r"(?:^|[^a-z])(?:knife|k)(?:[^a-z]|$)")))


def _item_kind(item):
    """Нож / пистолет / питомец по категории, названию или имени картинки; иначе None."""
    if item.get("category") == "pets":
        return "pet"
    name = str(item.get("name") or "").lower()
    if re.search(r"\bgun\b", name):
        return "gun"
    if re.search(r"\bknife\b", name):
        return "knife"
    stem = re.sub(r"\.[a-z0-9]+$", "", str(item.get("image") or "").rsplit("/", 1)[-1].lower())
    for kind, pattern in _KIND_WORDS:
        if stem and pattern.search(stem.replace("-", "_")):
            return kind
    return None


def _plain(text):
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def offer_fit(item, offer):
    """Насколько товар площадки подходит предмету: число (меньше — лучше) или None —
    точно другой предмет. Учитываются редкость (категория), год из происхождения,
    нож/пистолет/питомец, внутреннее имя картинки, есть ли цена, популярность и лоты."""
    score = 0.0
    category, own = expected_category(offer), item.get("category")
    if category and category != own:
        if category in _LADDER and own in _LADDER and abs(_LADDER.index(category) - _LADDER.index(own)) == 1:
            score += 60
        else:
            return None
    if not category:
        if rarity(offer.get("rarity")) == "event":
            if own in _VALUABLE:
                return None  # праздничный Peppermint за 2 ₽ — не Godly Peppermint
            score += 10
        else:
            score += 5  # редкость неизвестна — товар с совпавшей редкостью лучше
    years, year = _item_years(item), offer.get("year")
    if years:
        score += -20 if year in years else (50 if year is not None else 10)
    elif year is not None:
        score += 3  # у предмета год не указан — скорее основной товар без года
    kind, offer_kind = _item_kind(item), str(offer.get("kind") or "").lower() or None
    if kind and offer_kind and kind != offer_kind:
        score += 40
    stem = _plain(re.sub(r"\.[a-z0-9]+$", "", str(item.get("image") or "").rsplit("/", 1)[-1]))
    if stem and stem in (_plain(offer.get("real_name")), _plain(offer.get("image_id"))):
        score -= 200  # одна и та же картинка в игре — тот самый предмет
    if offer.get("price") is None and offer.get("price_rub") is None:
        score += 2
    popularity = offer.get("popularity")
    score += popularity if isinstance(popularity, (int, float)) else 1.0
    stock = offer.get("stock")
    if isinstance(stock, int):
        score -= min(stock, 1000) / 1000.0  # больше лотов — основной вариант
    return score


def offer_summary(offer):
    """Коротко о товаре площадки — для подсказки «на площадке есть похожие» и ручной привязки."""
    summary = {field: offer.get(field) for field in ("name", "rarity", "kind", "year", "price", "price_rub", "currency", "url")}
    summary["id"] = str(offer.get("id"))
    return summary


def assign_offers(groups, links=None, explain=None):
    """Одноимённые предметы и товары одной группы: каждому предмету — самый подходящий
    товар, один товар — одному предмету. groups: [(предметы, товары)].
    links — {id предмета: id товара}, выбранные пользователем («Это он»): им — в первую очередь.
    explain — словарь: предметам без товара туда пишется, что на площадке есть с этим
    названием и почему не подошло. -> ({id предмета: товар}, сколько товаров не подошло)."""
    links = links or {}
    matched, unused = {}, 0
    for items, offers in groups:
        taken_items, taken_offers = set(), set()
        for i, item in enumerate(items):
            wanted = links.get(item_id(item))
            j = next((j for j, offer in enumerate(offers)
                      if wanted is not None and j not in taken_offers and str(offer.get("id")) == str(wanted)), None)
            if j is not None:
                taken_items.add(i)
                taken_offers.add(j)
                matched[item_id(item)] = dict(offers[j], linked=True)
        pairs = []
        for i, item in enumerate(items):
            for j, offer in enumerate(offers):
                fit = offer_fit(item, offer) if i not in taken_items and j not in taken_offers else None
                if fit is not None:
                    pairs.append((fit, i, j))
        pairs.sort()
        for fit, i, j in pairs:
            if i in taken_items or j in taken_offers:
                continue
            taken_items.add(i)
            taken_offers.add(j)
            matched[item_id(items[i])] = offers[j]
        unused += len(offers) - len(taken_offers)
        if explain is not None:
            for i, item in enumerate(items):
                if i in taken_items:
                    continue
                rows = []
                for j, offer in enumerate(offers):
                    fit = offer_fit(item, offer)
                    reason = "другая редкость" if fit is None else "подошёл другому предмету" if j in taken_offers else ""
                    rows.append((fit is None, fit or 0.0, dict(offer_summary(offer), reason=reason)))
                rows.sort(key=lambda row: (row[0], row[1]))
                explain[item_id(item)] = [row[2] for row in rows[:8]]
    return matched, unused


def find_item(index, offer):
    """Предмет Supreme Values для товара площадки или None."""
    name = str(offer.get("name") or "")
    kind = str(offer.get("kind") or "").lower()
    every = index.get("every") or {key: [item] for key, item in index["exact"].items()}
    if kind in ("gun", "knife", "pet"):  # "Flowerwood" с типом Gun — это "Flowerwood Gun"
        same = every.get(parser.name_key(f"{name} {kind}"))
        if same:
            return _pick(same, offer)
    same = every.get(parser.name_key(name))
    if same:
        return _pick(same, offer)
    item = index["loose"].get(market_key(name))
    return _pick([item], offer) if item else None


def item_id(item):
    return f"{item['category']}:{item['name']}"


# Товары, которые пользователь прислал ссылками («они продаются, а программа пишет „нет в
# продаже“»). Привязываются к предмету с этим названием сами, один раз — потом их можно
# отвязать или выбрать другой товар. Редкость — какой из одноимённых предметов это.
KNOWN_PRODUCTS = (
    ("Peppermint", "godly", (
        "https://starpets.gg/ru/mm2/shop/weapon/peppermint/279",
        "https://dreampets.gg/mm2/product/peppermint/70db199b-0248-4deb-ab53-0d7f3132e2cf")),
    ("Snowflake", "godly", (
        "https://starpets.gg/ru/mm2/shop/weapon/snowflake/918",
        "https://dreampets.gg/mm2/product/snowflake/264ba2f5-c2cb-43f5-a508-194ebb34f7bc")),
    ("Shadow", "vintage", (
        "https://starpets.gg/ru/mm2/shop/weapon/shadow/269",
        "https://dreampets.gg/mm2/product/shadow/d3537bed-0ca9-4c17-bd8d-7e41fd970ecd")),
    ("Prince", "vintage", (
        "https://starpets.gg/ru/mm2/shop/weapon/prince/178",
        "https://dreampets.gg/mm2/product/prince/f48efeba-9af7-4411-ae6d-f75f729ba0c5")),
    ("Phaser", "vintage", (
        "https://starpets.gg/ru/mm2/shop/weapon/phaser/125",
        "https://dreampets.gg/mm2/product/phaser/b3b45d93-df92-4fe9-8cc8-33f475d74cf3")),
    ("Golden", "vintage", (
        "https://starpets.gg/ru/mm2/shop/weapon/golden/590",
        "https://dreampets.gg/mm2/product/golden/e9a75d02-4186-4657-af2e-efb36e0202a2")),
    ("Ghost", "vintage", (
        "https://starpets.gg/ru/mm2/shop/weapon/ghost/458",
        "https://dreampets.gg/mm2/product/ghost/986f0a0f-f132-449a-beb4-f55441d9abf3")),
    ("Cowboy", "vintage", (
        "https://starpets.gg/ru/mm2/shop/weapon/cowboy/761",
        "https://dreampets.gg/mm2/product/cowboy/f7b41518-ff5c-4fe5-b411-60e4bcd7a515")),
)

_STARPETS_PRODUCT_RE = re.compile(r"^https?://(?:www\.)?starpets\.gg/(?:[a-z]{2}(?:-[a-z]{2})?/)?mm2/shop/"
                                  r"[^/?#]+/[^/?#]+/(\d+)(?:[/?#]|$)", re.I)
_DREAMPETS_PRODUCT_RE = re.compile(r"^https?://(?:www\.)?dreampets\.(?:gg|io)/mm2/product/", re.I)


def product_ref(url):
    """Ссылка на страницу товара -> (вид площадки, id товара) или None.
    https://starpets.gg/ru/mm2/shop/weapon/peppermint/279 -> ("starpets", "279"),
    https://dreampets.gg/mm2/product/cowboy/<uuid> -> ("dreampets", "<uuid>")."""
    text = str(url or "").strip()
    match = _STARPETS_PRODUCT_RE.match(text)
    if match:
        return "starpets", match.group(1)
    if _DREAMPETS_PRODUCT_RE.match(text) and dreampets.uuid_of(text):
        return "dreampets", dreampets.uuid_of(text)
    return None


_YEAR_RE = re.compile(r"(?<!\d)(20[0-4]\d)(?!\d)")


def _item_years(item):
    text = " ".join(str(item.get(field) or "") for field in ("name", "origin", "aliases"))
    return {int(year) for year in _YEAR_RE.findall(text)}


def _variant_rank(item, offer):
    """Насколько товар площадки подходит предмету (меньше — лучше): редкость, совпадение
    года из названия/происхождения, есть ли цена, точное название, популярность."""
    category = expected_category(offer)
    rarity_miss = 0 if category is None or category == item.get("category") else 1  # Rare-нож, а предмет Godly
    years = _item_years(item)
    year = offer.get("year")
    year_miss = 0 if not years else (0 if year in years else 1)
    exact = 0 if parser.name_key(offer.get("name") or "") == parser.name_key(item["name"]) else 1
    priced = 0 if offer.get("price") is not None or offer.get("price_rub") is not None else 1
    popularity = offer.get("popularity")
    stock = offer.get("stock") if isinstance(offer.get("stock"), int) else 0  # больше лотов — основной вариант
    return (rarity_miss, year_miss, priced, exact, popularity if isinstance(popularity, (int, float)) else 1.0, -stock)


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
        # Товары, выбранные пользователем (ссылкой или «Это он»): {id товара: адрес его страницы}.
        # Их цена нужна, даже если в общем списке площадки товар не попался.
        self.wanted = {}

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
    превысить лимит запросов (~50, иначе блокировка). Продаётся ли товар, решает
    более свежий из двух снимков, а цена из старого остаётся, только если не
    устарела: иначе рубли считаются по курсу самого товара (рубли/доллары его
    последней проверенной пары, а пока её нет — по курсу площадки, «≈»), а
    доллары до следующего опроса не показываются (окно пересчитает их по курсу
    Rapira, «≈»).
    products/{id}/info —
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
        swept = []  # валюты, прочитанные в этом опросе
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
            # prev — прошлый снимок этой валюты: по нему видно, менялась ли цена,
            # пока читалась другая валюта.
            old = prices.get(currency, {})
            prices[currency] = {"at": now, "ok_at": now,
                                "rows": {key: row.get("price") for key, row in rows.items()},
                                "prev": old.get("rows") or {}, "prev_at": self._read_at(old)}
            swept.append(currency)
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
        offers = self._offers(rows_by_id, prices, swept)
        if swept and not blocked_seen:
            self._linked_lots(api, offers, "usd" if "usd" in swept else swept[-1])
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

    _ROW_FIELDS = ("id", "name", "type", "subtype", "year", "chroma", "rare", "realName", "imageId", "imageUri",
                   "_rank", "_type")

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

    SLACK = 0.02  # рубли и доллары товара расходятся с его курсом сильнее — цена старого снимка устарела

    @staticmethod
    def _read_at(entry):
        """Когда получены цены этой валюты (кэш прошлой версии хранил только at)."""
        return entry.get("ok_at", entry.get("at", 0) if entry.get("rows") else 0)

    def _offers(self, rows, prices, swept=()):
        usd, rub = prices.get("usd", {}), prices.get("rub", {})
        usd_rows, rub_rows = usd.get("rows", {}), rub.get("rows", {})
        rub_stale = self._rub_stale(prices)
        # Курс самой площадки (медиана рубли/доллары) — для товаров, чей собственный курс
        # ещё не известен.
        ratios = sorted(_price(rub_rows[k]) / _price(usd_rows[k]) for k in usd_rows
                        if _price(usd_rows.get(k)) and _price(rub_rows.get(k)) and _price(usd_rows[k]) >= 1)
        site_rate = ratios[len(ratios) // 2] if ratios else None
        # Собственный курс товара — рубли/доллары последней проверенной пары цен: у многих он
        # заметно отличается от медианы. По нему проверяется цена из старого снимка и считаются
        # рубли, которых нет или которые устарели (такие — «≈»).
        pairs, checked = self.cache.get("pairs") or {}, {}
        # За опрос читается одна валюта, поэтому снимки разного возраста. Более свежий решает,
        # продаётся ли товар; цена другой валюты из старого — только если она не устарела.
        usd_newer = self._read_at(usd) >= self._read_at(rub)
        new, old = (usd, rub) if usd_newer else (rub, usd)
        new_rows, old_rows = new.get("rows", {}), old.get("rows", {})
        together = "usd" in swept and "rub" in swept  # обе валюты прочитаны только что, подряд
        # Прошлые снимки обеих валют (свежей — сделанный до старого): если обе цены с них
        # не менялись, пара верна. Одна сменилась — лот мог смениться между снимками.
        prev = (new.get("prev") or {}) if new.get("prev_at", float("inf")) <= self._read_at(old) else {}
        old_prev = old.get("prev") or {}
        offers = []
        for key, row in rows.items():
            fresh, older = _price(new_rows.get(key)), _price(old_rows.get(key))
            own = pairs.get(key)
            rate = own or site_rate
            sure = verified = together
            if key in new_rows:
                available = fresh is not None
                if fresh is None:
                    older = None  # снят с продажи — старая цена другой валюты не в счёт
                elif older is not None and not together:
                    sure = verified = (key in prev and _price(prev[key]) == fresh
                                       and key in old_prev and _price(old_prev[key]) == older)
                    if not sure and rate:
                        expected = fresh * rate if usd_newer else fresh / rate
                        verified = abs(older - expected) <= self.SLACK * expected
                        if not verified:
                            older = None  # цена сменилась после старого снимка
            else:
                available = older is not None  # свежий снимок товара не видел — решает старый
            price, price_rub = (fresh, older) if usd_newer else (older, fresh)
            # Курс товара меняют только надёжные пары (иначе расхождения по 2% накапливались бы);
            # прошедшая проверку пара задаёт его, только если курса ещё нет.
            if price and price_rub and (sure or (verified and not own)):
                checked[key] = price_rub / price
            elif own:
                checked[key] = own
            approx = rub_stale and price_rub is not None
            rate = checked.get(key) or site_rate
            if price_rub is None and price is not None and rate:
                price_rub, approx = round(price * rate, 2), True
            offers.append(self._offer(row, price, price_rub, approx, available))
        self.cache["pairs"] = checked
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
            "rarity": rarity(row.get("rare")),
            "product": True,  # отдельный товар каталога: одноимённые — разные предметы
            "image_id": row.get("imageId"),
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

    BOOK = 72  # лотов за один запрос списка лотов

    def _linked_lots(self, api, offers, currency):
        """Товары, выбранные пользователем, у которых в каталоге нет цены: самый дешёвый лот и
        число лотов — из списка лотов самих товаров (как на их странице). Не вышло — не беда:
        остаётся «нет в продаже», повтор через час."""
        wanted = {str(key) for key in self.wanted or {}}
        need = [offer for offer in offers if str(offer["id"]) in wanted
                and offer["price"] is None and offer["price_rub"] is None]
        ids = []
        for offer in need[:12]:  # больше 12 товаров за запрос площадка не принимает
            try:
                ids.append(int(offer["id"]))
            except (TypeError, ValueError):
                pass
        if not ids or self.cache.get("book_off_until", 0) > time.time():
            return
        _pause(self.PAUSE)
        try:
            data = post_json(f"{api}/api/v2/store/items/product",
                             {"currency": currency, "products": ids, "amount": self.BOOK}, headers=self.HEADERS)
        except fetcher.FetchError:
            self.cache["book_off_until"] = time.time() + 3600
            return
        rows = data.get("items") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            self.cache["book_off_until"] = time.time() + 3600
            return
        lots = {}
        for row in rows:
            price = _price(row.get("price")) if isinstance(row, dict) else None
            if price:
                lots.setdefault(str(row.get("productId")), []).append(price)
        for offer in need:
            prices = lots.get(str(offer["id"]))
            if not prices:
                continue
            offer["price" if currency == "usd" else "price_rub"] = min(prices)
            offer["available"] = True
            if len(rows) < self.BOOK:  # список не обрезан — лотов столько и есть
                offer["stock"] = len(prices)

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
    4. страницы товаров — по две сразу, с паузой, по очереди (сначала давно не читанные):
       «от 328.98 ₽» и «674 лотов» из данных страницы, описания или основного блока;
       ошибка одной страницы не мешает остальным, 404 — товар снят с продажи;
    5. если площадка не пускает программу или рисует всё скриптом — скрытый Edge:
       открывает рынок как обычный посетитель, докручивает список и читает карточки.

    Если площадка не пускает (403/429/503) и ничего нового не прочитано — это ошибка
    BlockedError (пауза BLOCKED_PAUSE), а не «ок» со старыми ценами. Товар, которого
    давно не подтверждал ни один источник, не показывается.

    Если цен не нашлось, в папку debug/dreampets кладётся отчёт для проверки.
    """

    PAGES_PER_POLL = 60
    PAGES_TIME = 75  # секунд на страницы товаров за опрос (другие площадки ждут)
    BROWSER_PAGES = 60
    WORKERS = 2  # страниц сразу: площадку не перегружаем, остальное — в следующие опросы
    PAGE_TIMEOUT = 20
    PAUSE = 0.5
    SITEMAP_EVERY = 60 * 60
    BROWSER_EVERY = 10 * 60
    # Цены «как на сайте»: рынок DreamPets открывается в скрытом Edge и цена берётся из
    # того, что видит посетитель на карточке («от 328 ₽»), а не из данных внутри страницы
    # (там бывает другая валюта, старая или не минимальная цена). Такая цена главнее
    # остальных источников VISIBLE_FRESH секунд.
    VISIBLE_EVERY = 5 * 60
    VISIBLE_FRESH = 30 * 60
    VISIBLE_PAGES = 60  # страниц списка рынка («Показать ещё», ?page=2…) за проход
    VISIBLE_TIME = 150
    VISIBLE_MIN = 5  # меньше карточек с ценой — страница не дорисовалась, снимок не обновляется
    # Фильтры редкости рынка. Общий список отсортирован по популярности, и редкие товары
    # (Classic — винтажные Cowboy, Golden, Prince…) в нём далеко внизу; список по одному
    # фильтру короткий и читается целиком. За проход — сколько успеется, дальше по кругу.
    FILTERS = ("Classic", "Unique", "Ancient", "Christmas", "Halloween", "Legendary", "Rare", "Uncommon",
               "Common", "Godly")
    FILTER_TIME = 80
    FILTER_ONE = 40
    # Окна товаров, выбранных пользователем, которых нет среди карточек: «223 ₽ мин. цена»,
    # «24 на продаже».
    PRODUCT_PAGES = 12
    PRODUCT_TIME = 60
    PRODUCT_WAIT = 12
    MAX_SITEMAPS = 40
    USELESS_PAGES = 6  # прочитано столько страниц, и ни на одной нет ни цены, ни лотов — рисуются скриптом
    MISSING_DROP = 3  # страница товара — 404 столько раз подряд ...
    MISSING_TIME = 12 * 3600  # ... и не меньше столько времени с первого 404: запись удаляется
    GONE_KEEP = 3 * 24 * 3600  # удалённый товар столько помнится снятым (ссылка без цены его не возвращает) ...
    GONE_RECHECK = 6 * 3600  # ... но его страница раз в столько проверяется снова
    STALE_AFTER = 6 * 3600  # товар не подтверждён столько времени — его данные не показываются
    FORGET_AFTER = 7 * 24 * 3600  # ... а столько — запись удаляется из кэша
    API_FAILS = 3  # адрес данных из браузера столько раз подряд без товаров — забывается
    PAGES = "страницы товаров"
    _CARD_SELECTOR = 'a[href*="/mm2/product/"]'

    def __init__(self, config, cache=None):
        super().__init__(config, cache)
        # MarketMonitor подменяет своим: закрытие программы прерывает долгий опрос.
        self.stop_event = threading.Event()
        root = re.match(r"https?://[^/]+", str(config.get("url") or ""))
        self.root = root.group(0) if root else ""
        self._new_poll()

    def _new_poll(self):
        self.report = {"time": store.now_iso(), "base": self.config.get("url"), "steps": []}
        self.sources = {}
        self.fresh = set()  # товары, у которых цена и лоты уже получены в этом опросе
        self.fresh_price = set()  # товары с ценой из списка (рынок, данные, браузер) в этом опросе
        self.got_price = set()  # товары, цена которых записана в этом опросе (из любого источника)
        self.updated = set()  # товары, у которых в этом опросе обновились цена или лоты
        self._names = None  # (products, {name_key: ключ товара}) — строится раз за опрос

    def fetch(self):
        base = self.config["url"]
        self.root = re.match(r"https?://[^/]+", base).group(0)
        if self._visible(base):
            return self._fetch_visible(base)
        now = time.time()
        products = self.cache.setdefault("products", {})  # uuid -> данные товара
        self._migrate_cache(products, now)
        self._skip_downtime(products, now)
        self._new_poll()
        blocked = None

        market_html = None
        try:
            market_html = self._market_page(base)
        except fetcher.BlockedError as error:
            blocked = error
        if market_html:
            self._absorb_market(market_html, products, now)

        if not blocked:
            blocked = self._api_urls(base, products, now)

        if not blocked and not self._stopped() and (
                not products or now - self.cache.get("sitemap_at", 0) >= self.SITEMAP_EVERY):
            self._sitemaps(products)  # её отказ — не блокировка (см. _sitemaps)

        pages_useless = self.cache.get("pages_useless_until", 0) > now
        priced_now = sum(1 for key in products if key in self.fresh)
        need_browser = blocked or not products or (pages_useless and priced_now < len(products) / 2)
        visible = self._visible(base)
        every = self.VISIBLE_EVERY if visible else self.BROWSER_EVERY
        if (need_browser or visible) and not self._stopped() and now - self.cache.get("browser_at", 0) >= every:
            self.cache["browser_at"] = now
            # После 429 («слишком много запросов») страницы товаров не читаются и браузером —
            # только список; проверку 403/503 браузер может пройти, тогда и страницы.
            read_pages = bool(blocked) and not pages_useless and self._http_code(blocked) != 429
            try:
                self._browser_pass(base, products, now, read_pages)
            except Exception as error:  # браузер — запасной путь, его сбой не ломает опрос
                self._step("browser", "error", f"{type(error).__name__}: {error}")

        if not blocked and not pages_useless and not self._stopped():
            try:
                self._read_pages(products)
            except fetcher.BlockedError as error:
                blocked = error

        self._forget(products)
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
        if blocked and not self.updated:
            # Не пускают, и нового ничего: ошибка — опрос откладывается, а время последней
            # удачной проверки остаётся прежним (кэш сохранится и так).
            raise fetcher.BlockedError(f"DreamPets не пускает программу ({blocked}); цены — с прошлой проверки")
        if blocked:
            self.note += f" (часть данных не обновлена: {blocked})"
        return offers

    # --- общие части ---

    def _visible(self, base):
        """Брать цены «как на сайте» из браузера: на настоящем DreamPets, если есть Edge/Chrome
        (в markets.json можно выключить: "visible": false)."""
        visible = self.config.get("visible")
        if visible is None:
            visible = bool(re.search(r"//(?:www\.)?dreampets\.(?:gg|io)/", base))
        return bool(visible) and bool(fetcher.find_system_browser())

    def _fetch_visible(self, base):
        """Цены «как на сайте»: только то, что посетитель видит на карточках рынка
        (название, редкость, «от N ₽») и в окнах товаров («N ₽ мин. цена»). Рынок читается
        в скрытом Edge раз в VISIBLE_EVERY: весь список, затем списки по фильтрам редкости
        (по очереди), затем окна товаров, выбранных пользователем, если их не было среди
        карточек. Между проходами — последний снимок. Других источников нет: числа из
        данных страницы и со страниц товаров с экраном не совпадали."""
        now = time.time()
        self._new_poll()
        snap = self.cache.get("visible") or {}
        # Карточки со списков по фильтрам и из окон товаров: {ключ товара: карточка + "at"}.
        extra = {key: card for key, card in (snap.get("extra") or {}).items()
                 if isinstance(card, dict) and now - card.get("at", 0) <= self.VISIBLE_FRESH}
        wanted = {}
        for offer_id, url in (self.wanted or {}).items():
            uid = dreampets.uuid_of(offer_id) or dreampets.uuid_of(url)
            if uid and url:
                wanted[uid] = url
        known = set(extra)
        if now - snap.get("at", 0) <= self.VISIBLE_FRESH:
            known.update(self._card_key(card) for card in snap.get("cards") or [])
        due = now - snap.get("at", 0) >= self.VISIBLE_EVERY
        error = None
        if (due or set(wanted) - known) and not self._stopped():
            seen = set()  # товары, прочитанные в этом проходе
            try:
                with cdp.Browser() as browser:
                    if due:
                        browser.open(base, settle=2)
                        result = browser.collect_price_cards(timeout=self.VISIBLE_TIME)
                        cards = self._read_cards(result["cards"])
                        self._step("visible", base, f"карточек с ценой: {len(cards)}, шагов: {result.get('rounds')}, "
                                                    f"заголовок: {result.get('title')}")
                        if len(cards) >= self.VISIBLE_MIN:
                            snap = {"at": now, "cards": cards}
                            seen.update(self._card_key(card) for card in cards)
                            self._filter_cards(browser, extra, seen)
                        else:
                            error = f"на странице рынка нашлось карточек с ценой: {len(cards)}"
                    pages = [(uid, url) for uid, url in wanted.items() if uid not in seen and (due or uid not in known)]
                    self._product_pages(browser, pages, extra)
                    for note in browser.notes:
                        self._step("visible", "заметка", note)
            except Exception as failure:  # браузер не открылся, сайт не ответил — остаётся прошлый снимок
                error = f"{type(failure).__name__}: {failure}"
        snap = dict(snap, extra=extra)
        self.cache["visible"] = snap
        age = now - snap.get("at", 0)
        if not snap.get("cards") or age > self.VISIBLE_FRESH:
            self.report["visible_error"] = error
            self._save_report(None)
            raise fetcher.FetchError("не удалось прочитать цены с сайта DreamPets" + (f": {error}" if error else ""))
        # Один товар — одна цена: из более свежего чтения (список, фильтр или окно товара).
        # Одинаковые карточки без ссылки (то же название и редкость) — разные товары: #2, #3.
        entries, counts = {}, {}
        for card in snap["cards"]:
            key = self._card_key(card)
            counts[key] = counts.get(key, 0) + 1
            if counts[key] > 1 and not dreampets.uuid_of(key):
                key = f"{key}#{counts[key]}"
            entries[key] = (snap["at"], card)
        for key, card in extra.items():
            if key not in entries or card.get("at", 0) > entries[key][0]:
                entries[key] = (card.get("at", 0), card)
        offers = [self._visible_offer(key, card, base) for key, (_, card) in entries.items()]
        self.complete = True
        self.note = (f"цены как на сайте: {len(snap['cards'])} карточек"
                     + (f" и ещё {len(extra)} с фильтров и окон товаров" if extra else "")
                     + f", проверено {int(age // 60)} мин назад"
                     + (f" (последняя проверка не удалась: {error})" if error else ""))
        return offers

    @staticmethod
    def _card_key(card):
        """Ключ товара карточки: id из ссылки, иначе название и значок редкости."""
        uid = dreampets.uuid_of(card.get("href")) if card.get("href") else None
        return uid or "card:%s:%s" % (parser.name_key(card.get("name") or ""), str(card.get("rarity") or "").lower())

    @staticmethod
    def _read_cards(found):
        """Карточки со страницы (ссылка + текст) -> [{"name", "price", "lots", "rarity", "href"}] с ценой."""
        cards = []
        for card in found:
            slug = re.search(r"/product/([^/?#]+)", card.get("href") or "")
            data = dreampets.parse_card_text(card.get("text"), slug.group(1).replace("-", " ") if slug else None)
            if data.get("name") and data.get("price") is not None:
                cards.append({"name": data["name"], "price": data["price"], "lots": data.get("lots"),
                              "rarity": data.get("rarity"), "href": card.get("href") or ""})
        return cards

    @staticmethod
    def _visible_offer(key, card, base):
        price = card.get("price")
        return {
            "id": key,
            "name": card["name"],
            "price": price,
            "currency": "RUB",
            "stock": card.get("lots"),  # на карточках DreamPets лотов нет — None, а не 0
            "sales_week": None,
            "available": price is not None,
            "rarity": card.get("rarity"),
            "product": True,
            "price_from": price is not None,  # «от N ₽» — минимальная цена лота, как на карточке
            "url": card.get("href") or base,
        }

    def _filter_cards(self, browser, extra, seen):
        """Списки по фильтрам редкости — по очереди, сколько успеется за FILTER_TIME; в следующий
        проход — с того фильтра, на котором остановились."""
        order = self.FILTERS
        if not order:
            return 0
        first = int(self.cache.get("visible_filter") or 0) % len(order)
        start, done, clicked = time.time(), 0, 0
        while done < len(order) and time.time() - start < self.FILTER_TIME and not self._stopped():
            label = order[(first + done) % len(order)]
            done += 1
            if not browser.click_filter(label, order):
                self._step("visible", f"фильтр {label}", "не найден на странице")
                continue
            clicked += 1
            time.sleep(1.5)
            left = self.FILTER_TIME - (time.time() - start)
            found = browser.collect_price_cards(timeout=max(5, min(self.FILTER_ONE, left)), reset=True)
            cards = self._read_cards(found["cards"])
            at = time.time()
            for card in cards:
                key = self._card_key(card)
                extra[key] = dict(card, at=at)
                seen.add(key)
            self._step("visible", f"фильтр {label}", f"карточек с ценой: {len(cards)}")
            browser.click_filter(label, order)  # снять фильтр
            time.sleep(1)
        self.cache["visible_filter"] = (first + done) % len(order)
        return clicked

    def _product_pages(self, browser, pages, extra):
        """Окна товаров (по ссылке): «223 ₽ мин. цена», «24 на продаже» или «лотов нет»."""
        start = time.time()
        for uid, url in pages[:self.PRODUCT_PAGES]:
            if self._stopped() or time.time() - start > self.PRODUCT_TIME:
                break
            try:
                browser.open(url, settle=1)
                text, end = "", time.time() + self.PRODUCT_WAIT
                while True:
                    text = browser.page_text()
                    if dreampets.product_text_ready(text, dreampets.slug_name(url)) or time.time() > end:
                        break
                    time.sleep(0.7)
            except (cdp.BrowserError, TimeoutError, OSError) as error:
                self._step("visible", url, f"окно товара не прочитано: {error}")
                continue
            data = dreampets.parse_product_text(text, dreampets.slug_name(url))
            self._step("visible", url, f"окно товара: цена {data['price']}, лотов {data['lots']}"
                                       + (", лотов нет" if data["sold_out"] else ""))
            if data["price"] is None and not data["sold_out"]:
                continue  # окно не дорисовалось — остаётся прошлое чтение
            extra[uid] = {"name": data["name"] or dreampets.slug_name(url) or uid, "price": data["price"],
                          "lots": 0 if data["price"] is None else data["lots"], "rarity": data["rarity"],
                          "href": url, "at": time.time(), "page": True}

    def _visible_fresh(self, entry, now=None):
        seen = entry.get("visible_at")
        return bool(seen) and (now or time.time()) - seen < self.VISIBLE_FRESH

    def _step(self, name, what, result):
        self.report["steps"].append({"step": name, "what": str(what)[:300], "result": str(result)[:500]})

    def _count(self, source, number=1):
        self.sources[source] = self.sources.get(source, 0) + number

    def _stopped(self):
        return self.stop_event.is_set()

    @staticmethod
    def _http_code(error):
        """Код ответа HTTP из ошибки _request (429, 404...) или None."""
        code = getattr(getattr(error, "__cause__", None), "code", None)
        if isinstance(code, int):
            return code
        match = re.search(r"HTTP (\d{3})", str(error))
        return int(match.group(1)) if match else None

    def _migrate_cache(self, products, now=None):
        """Кэш прошлой версии хранил товары по ссылке — переводим на uuid; старые отметки
        «снят с продажи» (GONE_KEEP) выбрасываем."""
        for key in [k for k in products if k.startswith("http") and dreampets.uuid_of(k)]:
            entry = products.pop(key)
            if isinstance(entry, dict):
                entry.setdefault("url", self._absolute(key))
                products.setdefault(dreampets.uuid_of(key), entry)
        for key in [k for k, e in products.items() if not isinstance(e, dict)]:
            products.pop(key)
        for entry in products.values():
            entry.setdefault("seen", now or time.time())  # записи прошлой версии: отсчёт с этого запуска
        gone = self.cache.get("gone")
        if isinstance(gone, dict):
            self.cache["gone"] = {k: t for k, t in gone.items()
                                  if isinstance(t, (int, float)) and (now or time.time()) - t < self.GONE_KEEP}

    def _skip_downtime(self, products, now):
        """Время, когда программа была закрыта (или площадка долго не опрашивалась), не
        считается «товар не подтверждён»: иначе после ночи почти все цены пропали бы до
        конца круга по страницам. Отметки seen сдвигаются на этот перерыв."""
        last = self.cache.get("poll_at")
        interval = self.config.get("interval")
        interval = interval if isinstance(interval, (int, float)) and interval > 0 else POLL_EVERY
        pause = now - last - max(3 * interval, MAX_BACKOFF) if isinstance(last, (int, float)) else 0
        if pause > 0:
            for entry in products.values():
                if isinstance(entry.get("seen"), (int, float)):
                    entry["seen"] = min(now, entry["seen"] + pause)
        self.cache["poll_at"] = now

    def _forget(self, products):
        """Товары, которых неделю не подтверждал ни один источник, — из кэша."""
        now = time.time()
        for key in [k for k, e in products.items() if now - e.get("seen", now) > self.FORGET_AFTER]:
            products.pop(key)

    def _key(self, link):
        """Ключ товара: uuid из ссылки (одинаковый на .gg и .io), иначе сама ссылка."""
        return dreampets.uuid_of(link) or self._absolute(link)

    def _absolute(self, href):
        link = href if href.startswith("http") else self.root + href
        return re.sub(r"https?://[^/]+", self.root, link)  # одно зеркало, без двойного счёта

    def _named(self, products, wanted):
        """Ключ товара с таким name_key (или None) — по словарю названий, а не перебором."""
        if self._names is None or self._names[0] is not products:
            index = {}
            for key, entry in products.items():
                if entry.get("name"):
                    index.setdefault(parser.name_key(entry["name"]), key)
            self._names = (products, index)
        key = self._names[1].get(wanted)
        if key is not None and (key not in products or parser.name_key(products[key].get("name") or "") != wanted):
            self._names = None  # запись удалили или переименовали — словарь заново
            return self._named(products, wanted)
        return key

    def _index_name(self, products, key, replace=False):
        if self._names is not None and self._names[0] is products and products[key].get("name"):
            name_key = parser.name_key(products[key]["name"])
            if replace:
                self._names[1][name_key] = key
            else:
                self._names[1].setdefault(name_key, key)

    def _keeps_name(self, entry, name, source):
        """True — название записи не менять на name.

        То же название, лучше записанное ("Eternal III" вместо "eternal iii"), — всегда можно.
        Ссылка и заголовок страницы товара известный товар не переименовывают. Название
        из списка заменяет название из ссылки, только если подходит к ней: то же или длиннее
        («Chroma Harvester» для /harvester/), но не короче («Harvester» для /chroma-harvester/
        — значок Chroma потерян) и не другое («Knives» — раздел, ник продавца). Совпадающее
        со ссылкой название, наоборот, исправляет неподходящее."""
        current = entry.get("name")
        if not current and source not in ("slug", self.PAGES):
            current = dreampets.slug_name(entry.get("url"))  # новая запись: название ссылки уже есть
        if not current:
            return False
        new, old = parser.name_key(name), parser.name_key(current)
        if new == old:
            return False
        if source in ("slug", self.PAGES):
            return True
        slug = dreampets.slug_name(entry.get("url"))
        if not slug:
            return False
        slug_key = parser.name_key(slug)
        if new == slug_key:
            return False
        if old == slug_key and current != slug:
            return True
        return slug_key not in new

    def _upsert(self, products, data, now, source, link=None, key=None):
        """Добавить или обновить товар: по ссылке (uuid), иначе по названию."""
        if key is None and link:
            key = self._key(link)
        if key is None and data.get("id") is not None:
            key = dreampets.uuid_of(str(data["id"]))
        name = dreampets.clean_name(data.get("name")) if data.get("name") else None
        by_name = key is None
        if by_name:
            if not name:
                return None
            wanted = parser.name_key(name)
            key = self._named(products, wanted) or "name:" + wanted
        gone = self.cache.get("gone") or {}
        recheck = False
        if key in gone:
            if data.get("price") is not None or data.get("lots") is not None:
                gone.pop(key)  # снова продаётся
            elif key not in products:
                # Снят с продажи (404), а ссылка осталась, например, в карте сайта: раз в
                # GONE_RECHECK страница проверяется снова (одна страница; 404 — опять снят).
                if time.time() - gone[key] < self.GONE_RECHECK:
                    return None
                recheck = True
        entry = products.setdefault(key, {"at": 0})
        if recheck:
            entry.update(at=time.time(), page_at=0)  # данных нет, но и ждать нечего — страница первой в очереди
        entry["seen"] = time.time()  # источник подтвердил, что товар есть
        if link:
            entry["url"] = self._absolute(link)
        if rarity(data.get("rarity")):
            entry["rarity"] = rarity(data.get("rarity"))
        elif not entry.get("rarity") and link:
            # /mm2/product/laser-vintage/<uuid>: редкость словом в ссылке (если она там есть)
            slug = re.search(r"/product/([^/?#]+)", str(link))
            words = slug.group(1).lower().split("-") if slug else []
            entry["rarity"] = next((w for w in words if w in RARITY_CATEGORY), None)
        if name and not self._keeps_name(entry, name, source):
            entry["name"] = name
            self._index_name(products, key)
        elif not entry.get("name") and link:
            entry["name"] = dreampets.slug_name(link)
            self._index_name(products, key)
        if not key.startswith("name:") and entry.get("name"):
            # Тот же товар мог прийти раньше без ссылки (по названию) — сливаем в одну запись.
            twin = "name:" + parser.name_key(entry["name"])
            if twin in products and twin != key:
                old = products.pop(twin)
                self._index_name(products, key, replace=True)
                if old.get("at", 0) > entry.get("at", 0):  # новые данные ниже всё равно перезапишут
                    for field in ("price", "lots", "at", "source"):
                        if old.get(field) is not None:
                            entry[field] = old[field]
                for marks in (self.fresh, self.fresh_price, self.got_price, self.updated):
                    if twin in marks:
                        marks.discard(twin)
                        marks.add(key)
        price, lots = data.get("price"), data.get("lots")
        if source != "браузер" and self._visible_fresh(entry):
            price = None  # цена с экрана сайта свежая — данные страницы её не меняют
            if entry.get("visible_lots"):
                lots = None  # и число лотов, если карточка его показывала
        if by_name and price is not None and key in self.got_price and entry.get("price") is not None:
            # Совпадение только по названию (другой лот или вариант) не поднимает цену этого опроса.
            price = min(price, entry["price"])
        if by_name and key in self.fresh:
            lots = None  # цена и лоты этого опроса уже есть (те же данные из второго источника — не новые)
        if price is not None or lots is not None:
            entry.pop("missing", None)  # страница снова есть (или товар снова в списке)
            entry.pop("missing_since", None)
        if lots == 0:
            # Лотов нет — не продаётся, какую бы цену ни показывал источник (последнюю, старую).
            price = entry["price"] = None
            entry["at"] = now or time.time()
        if price is not None:
            entry["price"] = price
            entry["at"] = now
            entry["source"] = source
            self.got_price.add(key)
            if source != self.PAGES:
                self.fresh_price.add(key)  # цена со списка свежее страницы товара
            if data.get("lots") is not None:
                self.fresh.add(key)
            self._count(source)
        if lots is not None:
            entry["lots"] = lots
            entry["at"] = entry.get("at") or now
        if price is not None or lots is not None:
            self.updated.add(key)
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
        """Карточки со ссылками: весь текст карточки вокруг ссылки на товар — название,
        цена и лоты могут стоять и до ссылки, и после, и внутри неё.

        У одной ссылки бывает несколько карточек (подборка вверху и сетка): берётся та, чьё
        название подходит к ссылке, потом — где больше данных, потом — меньшая; недостающее
        берётся из карточек с тем же названием."""
        found = {}
        for path, fragment in dreampets.card_fragments(page_html):
            link = self._absolute(path)
            slug = dreampets.slug_name(link)
            card = dreampets.parse_card_text(dreampets.visible_text(fragment), slug)
            fit = dreampets.slug_fit(card["name"], slug) if card["name"] else 1
            filled = sum(card[field] is not None for field in ("price", "lots"))
            found.setdefault(link, []).append((-fit, -filled, len(fragment), card))
        cards = {}
        for link, variants in found.items():
            variants.sort(key=lambda variant: variant[:3])
            best = dict(variants[0][3])
            for *_, card in variants[1:]:
                if card["name"] is None or best["name"] is None or \
                        dreampets.same_name(card["name"]) == dreampets.same_name(best["name"]):
                    for field in ("name", "price", "lots"):
                        if best[field] is None:
                            best[field] = card[field]
            best["name"] = best["name"] or dreampets.slug_name(link)
            cards[link] = best
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

    def _api_urls(self, base, products, now):
        """Адреса данных, которые нашёл браузер. Адрес, который без браузера не открывается
        (403 — нужны его cookies) или перестал отдавать товары, — не блокировка площадки:
        после API_FAILS неудач подряд он просто забывается. 429/503 — не неудача адреса, а
        просьба площадки подождать: возвращается эта ошибка (иначе None)."""
        urls = self.cache.get("api_urls") or []
        fails = {url: n for url, n in (self.cache.get("api_fails") or {}).items() if url in urls}
        blocked = None
        for url in list(urls)[:5]:
            if self._stopped():
                break
            try:
                found = self._absorb_json(get_json(url, headers={"Origin": self.root, "Referer": base}), products, now,
                                          "api")
                result = "товаров нет"
            except fetcher.NetworkError as error:
                self._step("api", url, str(error))
                continue  # нет связи — адрес тут ни при чём
            except fetcher.FetchError as error:  # и BlockedError
                if isinstance(error, fetcher.BlockedError) and self._http_code(error) in (429, 503):
                    # «Слишком много запросов» — адрес рабочий, просто не сейчас; и остальным
                    # запросам этого опроса лучше подождать.
                    self._step("api", url, str(error))
                    blocked = error
                    break
                found, result = 0, str(error)
            if found:
                fails.pop(url, None)
                continue
            fails[url] = fails.get(url, 0) + 1
            self._step("api", url, f"{result} (неудач подряд: {fails[url]})")
            if fails[url] >= self.API_FAILS:
                urls.remove(url)
                fails.pop(url)
        if fails or "api_fails" in self.cache:
            self.cache["api_fails"] = fails
        return blocked

    # --- карта сайта ---

    def _sitemaps(self, products):
        """Карта сайта. Её отказ (403/429/503) — не блокировка площадки: страницы товаров
        и рынок от этого не перестают читаться. 403 (так отвечают и на несуществующий файл)
        — пропустить этот файл; 429/503 — карту дочитать в следующий раз (через SITEMAP_EVERY)."""
        from urllib.parse import urljoin  # noqa: E402 (нужен только здесь)

        def absolute(url):  # "Sitemap: /sitemap.xml" бывает и относительным
            url = urljoin(self.root + "/", url.strip())
            return url if re.match(r"https?://", url, re.I) else None

        def get(url, raw=False):
            try:
                return _request(url, headers=HTML_HEADERS, timeout=self.PAGE_TIMEOUT, raw=raw)
            except fetcher.FetchError:
                raise
            except Exception as error:  # оборванный ответ, неверный адрес — ошибка этого файла, а не опроса
                raise fetcher.FetchError(f"{type(error).__name__}: {error}") from error

        queue, stop = [], None
        try:
            queue += dreampets.sitemap_urls_from_robots(get(self.root + "/robots.txt"))
        except fetcher.FetchError as error:  # и BlockedError
            self._step("robots", self.root + "/robots.txt", str(error))
            if isinstance(error, fetcher.BlockedError) and self._http_code(error) != 403:
                stop = error
        queue = [url for url in map(absolute, queue) if url]
        for default in (self.root + "/sitemap.xml", self.root + "/sitemap_index.xml"):
            if default not in queue:
                queue.append(default)
        seen, found = set(), 0
        while queue and not stop and len(seen) < self.MAX_SITEMAPS and not self._stopped():
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                raw = get(url, raw=True)
            except fetcher.FetchError as error:  # и BlockedError
                self._step("sitemap", url, str(error))
                if isinstance(error, fetcher.BlockedError) and self._http_code(error) != 403:
                    stop = error  # «не спешите» — остальное в следующий раз
                continue
            children, pages = dreampets.parse_sitemap(raw)
            children = [url for url in map(absolute, children) if url]
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
        """Страницы товаров, сначала давно не читанные: не больше PAGES_PER_POLL страниц
        и PAGES_TIME секунд за опрос, по WORKERS сразу с паузой PAUSE. Ответ 403/429/503
        останавливает чтение до следующего опроса, закрытие программы — сразу."""
        import queue  # noqa: E402 (нужен только здесь)
        waiting = [key for key, entry in products.items() if key not in self.fresh and entry.get("url")
                   and not (self._visible_fresh(entry) and entry.get("visible_lots"))]
        due = sorted(waiting, key=lambda k: (products[k].get("page_at", 0), -(products[k].get("price") or 0)))
        due = due[:self.PAGES_PER_POLL]
        if not due:
            return
        jobs, results = queue.Queue(), queue.Queue()
        for key in due:
            jobs.put((key, products[key]["url"]))
        halt = threading.Event()  # площадка просит остановиться — остальные страницы в следующий раз
        deadline = time.time() + self.PAGES_TIME

        def read():
            while not halt.is_set() and not self._stopped() and time.time() < deadline:
                try:
                    key, url = jobs.get_nowait()
                except queue.Empty:
                    return
                try:
                    results.put((key, _request(url, headers=HTML_HEADERS, timeout=self.PAGE_TIMEOUT), None))
                except fetcher.BlockedError as error:
                    halt.set()
                    results.put((key, None, error))
                except fetcher.FetchError as error:
                    results.put((key, None, error))
                except Exception as error:  # обрыв ответа и т. п. — ошибка этой страницы, а не опроса
                    results.put((key, None, fetcher.FetchError(f"{type(error).__name__}: {error}")))
                _pause(self.PAUSE)

        # Потоки-демоны, а не пул: при закрытии программы их не ждут (запрос может висеть PAGE_TIMEOUT).
        workers = [threading.Thread(target=read, daemon=True) for _ in range(min(self.WORKERS, len(due)))]
        for worker in workers:
            worker.start()
        read_ok = useful = 0
        failures, blocked = [], None
        while not self._stopped():
            try:
                key, page_html, error = results.get(timeout=0.2)
            except queue.Empty:
                if time.time() > deadline + self.PAGE_TIMEOUT:
                    # Ответ, который приходит по байту, держит поток дольше PAGE_TIMEOUT: его не
                    # ждём (поток-демон закончится сам, его результат никто не прочтёт).
                    halt.set()
                    failures.append("страница отвечает слишком долго")
                    break
                if any(worker.is_alive() for worker in workers) or not results.empty():
                    continue
                break
            entry = products.get(key)
            if entry is None:
                continue
            entry["page_at"] = time.time()  # в конец очереди — и при ошибке
            if error is not None:
                failures.append(str(error))
                if isinstance(error, fetcher.BlockedError):
                    blocked = blocked or error
                elif self._http_code(error) in (404, 410) and key not in self.fresh_price:
                    self._not_found(products, key)
                else:
                    entry["at"] = entry.get("at") or time.time()  # не прочитана, но очередь до неё дошла
                continue
            read_ok += 1
            if self._apply_page(products, key, page_html) == "useful":
                useful += 1
        self._step("pages", f"{len(due)} в очереди", f"прочитано {read_ok}, с ценой или лотами {useful}, "
                   f"ошибок {len(failures)}" + (f": {failures[-1]}" if failures else ""))
        if read_ok >= self.USELESS_PAGES and not useful:
            # Страницы товаров рисуются скриптом: обычным запросом цен не прочитать.
            self.cache["pages_useless_until"] = time.time() + 6 * 3600
        if blocked:
            raise blocked

    def _apply_page(self, products, key, page_html):
        """Страница товара (ответ 200) -> данные записи; одно правило для обычного чтения и
        для скрытого браузера. Возвращает "useful" (цена или лоты), "phrase" (только надпись
        «Нет в наличии») или "empty" (ни цены, ни лотов)."""
        entry = products[key]
        entry.pop("missing", None)
        entry.pop("missing_since", None)
        (self.cache.get("gone") or {}).pop(key, None)  # повторная проверка снятого товара: страница есть
        parsed = dreampets.parse_product_page(page_html, entry.get("url"))
        if parsed["price"] is None and parsed["lots"] is None:
            entry["at"] = entry.get("at") or time.time()  # прочитана: на этой странице цены нет
            return "empty"
        result = "useful"
        if parsed.get("source") == "phrase":
            # Только надпись «Нет в наличии» без чисел: так выглядит и пустая заготовка
            # страницы, которую дорисовывает скрипт. Для проверки «страницы бесполезны»
            # это не данные, а цену со списка, API или браузера она не стирает — только
            # данные, прочитанные раньше с самой страницы.
            result = "phrase"
            if entry.get("source") not in (None, self.PAGES) or (entry.get("price") is None
                                                                  and entry.get("lots") is None):
                entry["at"] = entry.get("at") or time.time()
                return result
        # Заголовок страницы называет безымянный товар, но известный не переименовывает
        # (только пишет то же название лучше) — см. _keeps_name.
        data = {"lots": parsed["lots"], "name": parsed["name"]}
        if key in self.fresh_price:
            # Цена и наличие — со свежей карточки списка; со страницы — только число лотов.
            if parsed["lots"]:
                self.fresh.add(key)
            else:
                data["lots"] = None
        else:
            data["price"] = parsed["price"]  # None — цены на странице нет, прежняя остаётся (0 лотов — нет)
        self._upsert(products, data, time.time(), self.PAGES, key=key)
        return result

    def _not_found(self, products, key):
        """Страницы товара нет (404): не продаётся. Число лотов при этом неизвестно (None,
        а не 0): исчезнувшая страница — не купленные лоты. После MISSING_DROP раз подряд и
        не меньше MISSING_TIME с первого 404 запись удаляется, и ссылка без цены не
        возвращает её GONE_KEEP (список с ценой — вернёт; см. GONE_RECHECK)."""
        entry, now = products[key], time.time()
        entry["missing"] = entry.get("missing", 0) + 1
        entry.setdefault("missing_since", now)
        entry["price"], entry["lots"], entry["at"] = None, None, now
        self.updated.add(key)
        gone = self.cache.setdefault("gone", {})
        if key in gone or (entry["missing"] >= self.MISSING_DROP and now - entry["missing_since"] >= self.MISSING_TIME):
            products.pop(key)  # (key в gone — повторная проверка снятого товара: снова 404)
            gone[key] = now
            for marks in (self.fresh, self.fresh_price, self.got_price, self.updated):
                marks.discard(key)

    # --- скрытый браузер ---

    BROWSER_PAGE_LIMIT = 30  # страниц товаров за проход браузера: по одной, с паузой
    BROWSER_PAUSE = 1.0
    BROWSER_TIME = 60  # секунд на них — браузер закрывается как можно раньше
    _STATIC_RE = re.compile(r"\.(js|css|png|jpe?g|webp|gif|svg|ico|woff2?|ttf)(\?|$)", re.I)

    def _browser_pass(self, base, products, now, read_pages):
        pages = {}
        with cdp.Browser() as browser:
            try:
                browser.open(base, settle=1)
                # Постраничный рынок: прошлый проход остановился на странице "browser_next" — после
                # первой страницы продолжаем с неё, так за несколько проходов читаются все страницы.
                visible = self._visible(base)
                if visible:  # весь список рынка, как его листает посетитель
                    result = browser.collect_cards(self._CARD_SELECTOR, resume=self.cache.get("browser_next"),
                                                   timeout=self.VISIBLE_TIME, max_pages=self.VISIBLE_PAGES)
                else:
                    result = browser.collect_cards(self._CARD_SELECTOR, resume=self.cache.get("browser_next"))
                self.cache["browser_next"] = result.get("next")
                cards = result["cards"]
                for card in cards:
                    link = dreampets.PRODUCT_PATH_RE.search(card.get("href") or "")
                    if not link:
                        continue
                    data = dreampets.parse_card_text(card.get("text"))
                    if card.get("inner") and any(value is None for value in data.values()):
                        # "<i>3</i> лотов": число и слово — разные надписи; их склеивает innerText.
                        extra = dreampets.parse_card_text(card["inner"])
                        data = {field: extra.get(field) if value is None else value for field, value in data.items()}
                    key = self._upsert(products, data, now, "браузер", link.group(1))
                    if visible and key in products and data.get("price") is not None:
                        products[key]["visible_at"] = now  # цена — как её видит посетитель
                        products[key]["visible_lots"] = data.get("lots") is not None
                self._step("browser", base, f"карточек: {len(cards)}, страниц: {result.get('pages')}, "
                                            f"заголовок: {result.get('title')}")
                # Запросы, которые сделала сама страница: если в ответе товары с ценами —
                # запоминаем адрес и дальше читаем его напрямую.
                api = [u for u in result.get("api") or [] if not self._STATIC_RE.search(u.split("#")[0])]
                own = re.match(r"https?://[^/]+", base).group(0)
                api.sort(key=lambda u: not u.startswith(own))  # сначала адреса самой площадки
                # 403 одного адреса (например, «кто я» для гостя) — не запрет площадки: идём дальше.
                responses, stop = (browser.fetch_many(api[:20], pause=0.3, deadline=40, stop_on=(429, 503))
                                   if api else ({}, None))
                if stop:
                    self._step("browser-api", f"прочитано {len(responses)} из {len(api[:20])}", stop)
                useful = []
                for url, (status, text) in responses.items():
                    if status != 200:
                        continue
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
                    # Обычные запросы не пускают — страницы товаров через браузер (с его cookies):
                    # по одной, с паузой и до первого отказа площадки (403/429/503).
                    due = [e["url"] for k, e in sorted(products.items(), key=lambda kv: kv[1].get("page_at", 0))
                           if e.get("url") and k not in self.fresh][:min(self.BROWSER_PAGES, self.BROWSER_PAGE_LIMIT)]
                    if due:
                        pages, stop = browser.fetch_many(due, pause=self.BROWSER_PAUSE, deadline=self.BROWSER_TIME)
                        self._step("browser-pages", f"{len(due)} в очереди",
                                   f"прочитано {len(pages)}" + (f", остановка: {stop}" if stop else ""))
            finally:
                for note in browser.notes:
                    self._step("browser", "заметка", note)
        # Браузер уже закрыт — разбираем страницы товаров.
        for url, (status, text) in pages.items():
            key = self._key(url)
            if key not in products:
                continue
            if status == 200:
                self._apply_page(products, key, text)  # так же, как обычное чтение страниц
            elif status in (404, 410) and key not in self.fresh_price:
                self._not_found(products, key)
            if key in products and status not in (403, 429, 503):
                # В конец очереди — и при ошибке (500, нет ответа): иначе такая страница вечно
                # стояла бы первой. Только отказ площадки, остановивший чтение, — не в счёт.
                products[key]["page_at"] = time.time()

    # --- результат ---

    def _offers(self, products):
        offers = []
        now = time.time()
        for key, entry in products.items():
            lots = entry.get("lots")
            price = entry.get("price")
            missing = bool(entry.get("missing"))  # страницы нет (404): не продаётся, лоты неизвестны
            if not entry.get("name") or (price is None and lots is None and not missing):
                continue  # ещё не прочитан — данных нет (это не «нет в продаже»)
            if now - entry.get("seen", now) > self.STALE_AFTER:
                continue  # давно не подтверждён ни списком, ни картой сайта, ни страницей — данные устарели
            offers.append({
                "id": key,
                "name": entry["name"],
                "price": None if missing else price,
                "currency": "RUB",
                "stock": None if missing else lots,  # не 0: исчезнувшие лоты — не покупки
                "sales_week": None,
                "available": False if missing else bool(lots) if lots is not None else price is not None,
                "rarity": entry.get("rarity"),
                "product": True,  # отдельная страница товара: одноимённые — разные варианты, их цены не смешивать
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
        self.links = {}        # id предмета -> id товара, выбранный пользователем («Это он», ссылка)
        self.urls = {}         # id выбранного товара -> адрес его страницы (окно товара читается по нему)
        self.seeded = set()    # товары из KNOWN_PRODUCTS, уже привязанные (повторно не привязываются)
        self.candidates = {}   # id предмета без товара -> одноимённые товары площадки и почему не подошли

    def interval(self, default):
        return self.config.get("interval") or default

    def public(self, interval):
        return {
            "id": self.config["id"],
            "title": self.config["title"],
            "kind": self.config.get("kind"),
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
            links = entry.get("links") if isinstance(entry.get("links"), dict) else {}
            market.links = {str(k): str(v) for k, v in links.items() if v is not None}
            urls = entry.get("urls") if isinstance(entry.get("urls"), dict) else {}
            market.urls = {str(k): str(v) for k, v in urls.items() if isinstance(v, str)}
            seeded = entry.get("seeded") if isinstance(entry.get("seeded"), list) else []
            market.seeded = {str(v) for v in seeded}
            candidates = entry.get("candidates") if isinstance(entry.get("candidates"), dict) else {}
            market.candidates = {str(k): v for k, v in candidates.items() if isinstance(v, list)}
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
                    "links": market.links,
                    "urls": market.urls,
                    "seeded": sorted(market.seeded),
                    "candidates": market.candidates,
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
        cdp.close_all()  # скрытый Edge/Chrome прохода DreamPets — сразу, не дожидаясь конца опроса

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
        adapter = factory(market.config, market.cache) if factory else make_adapter(market.config, market.cache)
        adapter.stop_event = self._stop  # закрытие программы прерывает долгий опрос (страницы DreamPets)
        return adapter

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
        index = build_index(items)
        by_key = {item_id(item): item for item in items}
        adapter = self._adapter(market)
        with self._lock:
            market.status = "busy"
            self._seed_links(market, index)
            # Выбранные пользователем товары: id товара -> id предмета (им — в первую очередь).
            links = {key: offer_id for key, offer_id in market.links.items() if key in by_key}
            pinned = {}
            for key, offer_id in links.items():
                pinned.setdefault(str(offer_id), key)
            adapter.wanted = {offer_id: market.urls.get(offer_id) for offer_id in pinned}
        try:
            offers = adapter.fetch()
        except Exception as error:
            self._fail(market, error)
            self._save_state()  # кэш адаптера мог пополниться и до ошибки
            return
        matched = {}
        unmatched = 0
        # Предметы, чей выбранный товар площадка сейчас показала (с ценой или без — «лотов нет»):
        # в подборе они не участвуют. Не показала (окно товара не прочиталось) — до следующего
        # опроса цена по обычному подбору, а не пусто.
        chosen = {pinned[str(offer.get("id"))] for offer in offers if str(offer.get("id")) in pinned}
        groups = {}  # товары-страницы (StarPets, DreamPets): одноимённые сопоставляются вместе
        for offer in offers:
            owner = pinned.get(str(offer.get("id")))
            if owner is not None:
                matched.setdefault(owner, dict(offer, linked=True))
                continue
            if offer.get("product"):
                candidates = candidate_items(index, offer)
                if not candidates:
                    unmatched += 1
                    continue
                group = groups.setdefault(tuple(item_id(c) for c in candidates), (candidates, []))
                group[1].append(offer)
                continue
            item = find_item(index, offer)
            if item is None or item_id(item) in chosen:  # у предмета товар выбран пользователем
                unmatched += 1
                continue
            key = item_id(item)
            previous = matched.get(key)
            if previous is None:
                matched[key] = dict(offer)
            elif (offer.get("id") is not None and previous.get("id") is not None and offer["id"] != previous["id"]
                  and (offer.get("year") is not None or previous.get("year") is not None
                       or offer.get("rarity") != previous.get("rarity")
                       or (offer.get("product") and previous.get("product")))):
                # Разные товары площадки с одним названием (Cane 2015 / 2018 / 2021, Laser
                # Godly / Rare-нож): не смешивать, а выбрать подходящий этому предмету.
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
        explain = {}
        assigned, unused = assign_offers([([item for item in same if item_id(item) not in chosen], group)
                                          for same, group in groups.values()], explain=explain)
        matched.update((key, dict(offer)) for key, offer in assigned.items())
        unmatched += unused
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
            market.candidates = {key: rows for key, rows in explain.items() if key not in market.offers}
            market.failures = 0
            market.status = "ok"
            market.message = adapter.note if offers else "площадка не вернула ни одного предложения"
            market.last_ok_at = now
            market.next_poll_at = now + timedelta(seconds=market.interval(self.interval))
            self.version += 1
        self._save_state()

    # --- ручная привязка («Это он») ---

    def link(self, market_id, item_key, offer_id, url=None):
        """Пользователь указал товар площадки для предмета (offer_id None — отвязать).
        Привязка запоминается. Товар из подсказок показывается сразу, другой (по ссылке) —
        после опроса площадки, который начинается сразу."""
        wake = False
        with self._lock:
            market = next((m for m in self.markets if m.config["id"] == market_id), None)
            if market is None:
                raise KeyError(market_id)
            if offer_id is None:
                market.links.pop(item_key, None)
                if (market.offers.get(item_key) or {}).get("linked"):
                    market.offers.pop(item_key, None)
            else:
                offer_id = str(offer_id)
                market.links[item_key] = offer_id
                shown = next((row for row in market.candidates.get(item_key) or [] if row.get("id") == offer_id), None)
                url = url or (shown or {}).get("url")
                if url and product_ref(url):
                    market.urls[offer_id] = url
                if shown:
                    market.offers[item_key] = dict(shown, available=True, linked=True)
                    market.candidates.pop(item_key, None)
                else:
                    previous = (market.offers.get(item_key) or {}).get("id")
                    if previous is not None and str(previous) != offer_id:
                        market.offers.pop(item_key, None)  # прежний товар — не этот
                    market.next_poll_at = _now()
                    wake = True
            self.version += 1
        self._save_state()
        if wake:
            self._wake.set()

    def link_url(self, item_key, url):
        """Привязать предмет к товару по ссылке на страницу товара StarPets или DreamPets.
        -> id площадки. ValueError — это не ссылка на товар, KeyError — площадка выключена."""
        ref = product_ref(url)
        if ref is None:
            raise ValueError("Нужна ссылка на страницу товара StarPets или DreamPets")
        kind, offer_id = ref
        market = next((m for m in self.markets if m.config.get("kind") == kind), None)
        if market is None:
            raise KeyError(kind)
        self.link(market.config["id"], item_key, offer_id, url=str(url).strip())
        return market.config["id"]

    def _seed_links(self, market, index):
        """Товары из KNOWN_PRODUCTS — к предметам с их названием (каждый один раз)."""
        for name, rarity_name, urls in KNOWN_PRODUCTS:
            same = index["every"].get(parser.name_key(name)) or []
            category = RARITY_CATEGORY.get(rarity_name)
            fit = [item for item in same if item.get("category") == category] or (same if len(same) == 1 else [])
            for url in urls:
                ref = product_ref(url)
                if not fit or not ref or ref[0] != market.config.get("kind") or ref[1] in market.seeded:
                    continue
                market.seeded.add(ref[1])
                key = item_id(fit[0])
                if key not in market.links:
                    market.links[key] = ref[1]
                    market.urls[ref[1]] = url

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
            candidates = {market.config["id"]: dict(market.candidates) for market in self.markets}
        for item in items:
            key = item_id(item)
            infos = {}
            for config, offers, sold in snapshot:
                offer = offers.get(key)
                if not isinstance(offer, dict):
                    shown = candidates.get(config["id"], {}).get(key)
                    if shown:  # товара нет, но одноимённые на площадке есть — показать, почему не подошли
                        item.setdefault("market_candidates", {})[config["id"]] = shown
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
                    "price_from": bool(offer.get("price_from")),
                    "linked": bool(offer.get("linked")),
                    "product": " · ".join(str(part) for part in (
                        offer.get("name"), offer.get("rarity"), offer.get("kind"), offer.get("year")) if part),
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
