"""Разбор страниц DreamPets (dreampets.gg): ссылки на товары, данные, встроенные в
страницу, страницы товаров, карты сайта.

У площадки нет описанного открытого API, а сайт может рисовать список товаров
скриптом, поэтому товары и цены ищутся всеми способами сразу: обычные ссылки,
ссылки внутри данных страницы (Next.js, Nuxt), JSON-LD, описание страницы,
видимый текст. Всё — стандартной библиотекой Python.
"""

import gzip
import html as html_lib
import json
import re

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_UUID_RE = re.compile(UUID, re.I)
# /mm2/product/<slug>/<uuid> или /mm2/product/<uuid>; "/mm2-legacy/" не подходит.
PRODUCT_PATH_RE = re.compile(
    r"(?:https?://(?:www\.)?dreampets\.(?:gg|io))?(/mm2/product/(?:[a-z0-9][a-z0-9-]*/)?" + UUID + r")(?![0-9a-f-])",
    re.I,
)


def uuid_of(link):
    match = _UUID_RE.search(str(link or ""))
    return match.group(0).lower() if match else None


def slug_name(link):
    """/mm2/product/eternal-iii/<uuid> -> "eternal iii" (регистр для сопоставления не важен)."""
    match = re.search(r"/mm2/product/([a-z0-9-]+)/" + UUID, str(link or ""), re.I)
    return match.group(1).replace("-", " ") if match else None


def _unescape_js(text):
    """\\/ -> /, \\u002F -> /, \\" -> " (ссылки внутри JSON и данных Next.js)."""
    text = text.replace("\\u002F", "/").replace("\\u002f", "/").replace("\\/", "/")
    return text.replace('\\\\"', '"').replace('\\"', '"')


def product_links(text, root="https://dreampets.gg"):
    """Все ссылки на товары MM2 в HTML/JS/JSON/XML — по порядку, без повторов (по uuid)."""
    found, seen = [], set()
    for match in PRODUCT_PATH_RE.finditer(_unescape_js(html_lib.unescape(text or ""))):
        path = match.group(1)
        uid = uuid_of(path)
        if uid in seen:
            continue
        seen.add(uid)
        found.append(root + path)
    return found


# --- данные, встроенные в страницу ----------------------------------------------

_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)


def _json_or_none(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _nuxt_unflatten(payload):
    """Nuxt 3 __NUXT_DATA__ (devalue): плоский массив, элементы ссылаются на индексы."""
    if not isinstance(payload, list) or not payload:
        return None
    memo = {}

    def hydrate(index, depth=0):
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(payload) or depth > 60:
            return None
        if index in memo:
            return memo[index]
        value = payload[index]
        if isinstance(value, list):
            if value and isinstance(value[0], str) and value[0] in ("Reactive", "ShallowReactive", "Ref",
                                                                   "ShallowRef", "EmptyRef"):
                result = hydrate(value[1], depth + 1) if len(value) > 1 else None
            elif value and isinstance(value[0], str) and value[0] in ("Set", "Map", "Date", "BigInt", "null",
                                                                     "undefined"):
                result = None
            else:
                result = []
                memo[index] = result
                result.extend(hydrate(i, depth + 1) for i in value)
                return result
        elif isinstance(value, dict):
            result = {}
            memo[index] = result
            for key, i in value.items():
                result[key] = hydrate(i, depth + 1)
            return result
        else:
            result = value
        memo[index] = result
        return result

    return hydrate(0)


def embedded_json(page_html):
    """JSON, встроенный в страницу: __NEXT_DATA__, данные Next.js app router,
    __NUXT_DATA__, JSON-LD, <script type="application/json">, window.__STATE__ = {...}."""
    blobs, flight = [], []
    for attrs, body in _SCRIPT_RE.findall(page_html or ""):
        body = body.strip()
        if not body:
            continue
        attrs_l = attrs.lower()
        if "application/ld+json" in attrs_l or "application/json" in attrs_l:
            data = _json_or_none(html_lib.unescape(body) if "&quot;" in body else body)
            if data is None:
                continue
            if "__nuxt_data__" in attrs_l:
                data = _nuxt_unflatten(data)
            blobs.append(data)
            continue
        for chunk in re.findall(r"self\.__next_f\.push\(\[\d+,\s*(\"(?:[^\"\\]|\\.)*\")\]\)", body, re.S):
            text = _json_or_none(chunk)
            if isinstance(text, str):
                flight.append(text)
        state = re.search(r"window\.__(?:INITIAL_STATE|PRELOADED_STATE|APOLLO_STATE|DATA|STATE|NUXT)__\s*=\s*(\{.*\})\s*;?\s*$",
                          body, re.S)
        if state:
            data = _json_or_none(state.group(1))
            if data is not None:
                blobs.append(data)
    for line in "".join(flight).split("\n"):
        match = re.match(r"^[0-9a-f]+:(?:[A-Z]+)?(\[.*\]|\{.*\})$", line.strip())
        if match:
            data = _json_or_none(match.group(1))
            if data is not None:
                blobs.append(data)
    return blobs


# --- объекты, похожие на товары ---------------------------------------------------

NAME_KEYS = ("name", "title", "nameEn", "name_en", "enName", "en_name", "displayName", "display_name")
PRICE_KEYS = ("minPrice", "min_price", "lowPrice", "low_price", "priceFrom", "price_from", "fromPrice", "from_price",
              "minimalPrice", "minimal_price", "bestPrice", "best_price", "price", "cost")
COUNT_KEYS = ("lotsCount", "lots_count", "lots", "lotCount", "lot_count", "offersCount", "offers_count", "offerCount",
              "offer_count", "itemsCount", "items_count", "count", "quantity", "stock", "amount", "total")
ID_KEYS = ("id", "uuid", "productId", "product_id", "_id")


def rub(text):
    """"328.98", "34,99", "1 299,50", "1,299.50", "12,345", "1.299,50" -> число рублей."""
    cleaned = re.sub(r"[\s  ₽]|руб\.?|RUB", "", str(text), flags=re.I).rstrip(".,")
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif re.fullmatch(r"\d{1,3}(?:[,.]\d{3})+", cleaned):
        cleaned = re.sub(r"[,.]", "", cleaned)
    else:
        cleaned = cleaned.replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value > 0 else None


def _number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and re.fullmatch(r"\s*[\d\s .,]+\s*(?:₽|руб\.?|RUB)?\s*", value, re.I):
        return rub(value)
    if isinstance(value, dict):  # {"amount": 328.98, "currency": "RUB"} / {"value": ...}
        for key in ("amount", "value", "rub", "RUB"):
            if key in value:
                return _number(value[key])
    return None


def _first(obj, keys, convert):
    for key in keys:
        if key in obj:
            value = convert(obj[key])
            if value is not None:
                return key, value
    return None, None


def clean_name(value):
    """Английская часть названия: "Eternal / Вечный" -> "Eternal"."""
    if isinstance(value, dict):  # {"en": "Harvester", "ru": "Сборщик"}
        value = value.get("en") or value.get("EN") or next((v for v in value.values() if isinstance(v, str)), None)
    if isinstance(value, str) and re.search(r"[A-Za-z]", value) and len(value) <= 80:
        return re.split(r"\s+(?:/|—|–)\s+", value.strip(), maxsplit=1)[0].strip() or None
    return None


def _count(value):
    if isinstance(value, list):
        return len(value)  # список лотов
    number = _number(value)
    return int(number) if number is not None and float(number).is_integer() else None


def walk_products(data, depth=0):
    """{"name", "price", "lots", "id", "slug", "keys"} для словарей, похожих на товары
    (латинское название + цена) — в данных любой формы."""
    if depth > 40:
        return
    if isinstance(data, dict):
        name_key, name = _first(data, NAME_KEYS, clean_name)
        price_key, price = _first(data, PRICE_KEYS, _number)
        if name and price is not None and price > 0:
            count_key, lots = _first(data, COUNT_KEYS, _count)
            _, pid = _first(data, ID_KEYS, lambda v: v if isinstance(v, (str, int)) and not isinstance(v, bool) else None)
            slug = data.get("slug") if isinstance(data.get("slug"), str) else None
            yield {"name": name, "price": price, "lots": lots, "id": pid, "slug": slug,
                   "keys": (name_key, price_key, count_key)}
        offers = data.get("offers")
        if name and isinstance(offers, dict) and price is None:  # JSON-LD Product + AggregateOffer
            low = _number(offers.get("lowPrice")) or _number(offers.get("price"))
            if low:
                yield {"name": name, "price": low, "lots": _count(offers.get("offerCount")), "id": None,
                       "slug": None, "keys": (name_key, "offers.lowPrice", "offers.offerCount")}
        for value in data.values():
            yield from walk_products(value, depth + 1)
    elif isinstance(data, list):
        for value in data:
            yield from walk_products(value, depth + 1)


# --- текст карточки и страница товара ---------------------------------------------

PRICE_FROM_RE = re.compile(r"от\s*(\d{1,3}(?:[   ,.]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s*(?:₽|руб)", re.I)
PRICE_RE = re.compile(r"(\d{1,3}(?:[   ,.]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s*(?:₽|руб)", re.I)
LOTS_RE = re.compile(r"(?<![\w.,])(\d{1,3}(?:[   ]\d{3})+|\d+)[  ]*лот|лот(?:ов|а)?\s*:\s*(\d+)", re.I)
_BUTTON_RE = re.compile(r"^(купить|в корзину|продать|подробнее|от|лот\w*|шт\.?|new|hot|sale|скидка.*)$", re.I)
_RARITY_RE = re.compile(r"^(common|uncommon|rare|legendary|godly|ancient|vintage|unique|chroma|pet|misc|"
                        r"обычн\w*|необычн\w*|редк\w*|легендарн\w*|древн\w*|винтажн\w*)$", re.I)


def lots_value(match):
    return int(re.sub(r"\D", "", match.group(1) or match.group(2))) if match else None


def parse_card_text(text):
    """Текст карточки товара -> {"name", "price", "lots"} (что удалось найти)."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(text or "").splitlines()]
    lines = [line for line in lines if line]
    price = None
    for number, line in enumerate(lines):
        match = PRICE_FROM_RE.search(line) or PRICE_RE.search(line)
        if not match and number + 1 < len(lines) and lines[number + 1].startswith("₽"):
            match = PRICE_RE.search(f"{line} {lines[number + 1]}")
        if match:
            price = rub(match.group(1))
            break
    lots = next((lots_value(m) for m in (LOTS_RE.search(line) for line in lines) if m), None)
    name = next((clean_name(line) for line in lines
                 if re.search(r"[A-Za-z]", line) and "₽" not in line and not LOTS_RE.search(line)
                 and not _BUTTON_RE.match(line) and not _RARITY_RE.match(line) and clean_name(line)), None)
    return {"name": name, "price": price, "lots": lots}


_META_RE = re.compile(r"<meta\b[^>]*>", re.I)


def _meta(page_html, names):
    out = []
    for tag in _META_RE.findall(page_html):
        key = re.search(r"(?:name|property)\s*=\s*[\"']([^\"']+)[\"']", tag, re.I)
        content = re.search(r"content\s*=\s*(\"[^\"]*\"|'[^']*')", tag, re.I)
        if key and content and key.group(1).lower() in names:
            out.append(html_lib.unescape(content.group(1)[1:-1]))
    return out


def parse_product_page(page_html):
    """{"name", "price", "lots", "source"} со страницы товара."""
    page_html = page_html or ""
    title = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.S | re.I)
    title_text = html_lib.unescape(re.sub(r"\s+", " ", title.group(1))).strip() if title else ""
    og_title = _meta(page_html, ("og:title",))
    raw_name = title_text if "купить" in title_text.lower() else (og_title[0] if og_title else "")
    name = re.split(r"\s+(?:/|—|–|-)\s+", raw_name, maxsplit=1)[0].strip() or None
    # 1) данные страницы (JSON-LD, встроенное состояние) — точные числа
    for blob in embedded_json(page_html):
        for product in walk_products(blob):
            if name is None or product["name"].lower() == name.lower():
                return {"name": name or product["name"], "price": product["price"], "lots": product["lots"],
                        "source": "json"}
    # 2) описание страницы ("... от 328.98 ₽, 674 лотов ..."), затем видимый текст
    texts = _meta(page_html, ("description", "og:description", "twitter:description"))
    body = re.sub(r"<(script|style)\b.*?</\1>", " ", page_html, flags=re.S | re.I)
    body = re.sub(r">(?=<)", "> ", body)
    texts += [html_lib.unescape(t).strip() for t in re.split(r"<[^>]+>", body) if t.strip()]
    price = next((m for m in (PRICE_FROM_RE.search(t) for t in texts) if m), None)
    lots = next((m for m in (LOTS_RE.search(t) for t in texts) if m), None)
    return {"name": name, "price": rub(price.group(1)) if price else None, "lots": lots_value(lots), "source": "text"}


# --- карты сайта ------------------------------------------------------------------

def sitemap_urls_from_robots(robots_txt):
    return re.findall(r"(?im)^\s*sitemap\s*:\s*(\S+)", robots_txt or "")


def parse_sitemap(raw):
    """-> (вложенные карты сайта, адреса страниц). Понимает gzip и <sitemapindex>."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            return [], []
    text = raw.decode("utf-8", "replace")
    locs = [html_lib.unescape(x.strip()) for x in re.findall(r"<loc>\s*(.*?)\s*</loc>", text, re.S | re.I)]
    if re.search(r"<sitemapindex\b", text, re.I):
        return locs, []
    return [], locs


# --- поиск API в скриптах (для отчёта о диагностике) --------------------------------

_API_HINT_RES = [
    re.compile(r"https?://[a-z0-9.-]*dreampets\.(?:gg|io)(?:/[^\s\"'`)<>]*)?", re.I),
    re.compile(r"https?://api\.[a-z0-9.-]+(?:/[^\s\"'`)<>]*)?", re.I),
    re.compile(r"[\"'`](/api/[A-Za-z0-9_./${}:-]+)[\"'`]"),
    re.compile(r"baseURL\s*:\s*[\"'`]([^\"'`]+)[\"'`]"),
]


def api_hints(js_text):
    hints = []
    for regex in _API_HINT_RES:
        for match in regex.finditer(js_text or ""):
            value = match.group(1) if regex.groups else match.group(0)
            if value not in hints and not re.search(r"\.(png|jpe?g|webp|svg|gif|woff2?|css)(\?|$)", value, re.I):
                hints.append(value)
    return hints[:100]
