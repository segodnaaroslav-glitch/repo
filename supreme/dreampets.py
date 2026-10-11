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
# Только ключи числа лотов: "count", "total", "quantity" и длина списка — это что угодно
# (отзывы, вложенные лоты на странице), но не число лотов товара.
COUNT_KEYS = ("lotsCount", "lots_count", "lotCount", "lot_count", "lotsTotal", "lots_total", "totalLots", "total_lots",
              "offersCount", "offers_count", "offerCount", "offer_count", "lots")
# Списки внутри товара — его лоты и предложения, а не другие товары.
LOT_KEYS = ("lots", "offers", "listings", "sellers", "orders", "trades")
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
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())  # "0" — тоже число (rub() его не примет)
    number = _number(value)  # список (сами лоты) — не число лотов
    return int(number) if number is not None and float(number).is_integer() else None


def same_name(name):
    """Ключ для сравнения названий: "Eternal III" == "eternal-iii"."""
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


def walk_products(data, depth=0, parent=None):
    """{"name", "price", "lots", "id", "slug", "keys"} для словарей, похожих на товары
    (латинское название + цена) — в данных любой формы.

    Внутрь найденного товара за его лотами (LOT_KEYS) не заходит, а вложенные записи
    с тем же названием — его лоты или варианты, а не отдельные товары: иначе цена
    последнего лота заменила бы минимальную цену товара."""
    if depth > 40:
        return
    if isinstance(data, dict):
        name_key, name = _first(data, NAME_KEYS, clean_name)
        price_key, price = _first(data, PRICE_KEYS, _number)
        found = None
        if name and price is not None and price > 0:
            count_key, lots = _first(data, COUNT_KEYS, _count)
            _, pid = _first(data, ID_KEYS, lambda v: v if isinstance(v, (str, int)) and not isinstance(v, bool) else None)
            slug = data.get("slug") if isinstance(data.get("slug"), str) else None
            found = {"name": name, "price": price, "lots": lots, "id": pid, "slug": slug,
                     "keys": (name_key, price_key, count_key)}
        offers = data.get("offers")
        if name and isinstance(offers, dict) and price is None:  # JSON-LD Product + AggregateOffer
            low = _number(offers.get("lowPrice")) or _number(offers.get("price"))
            if low:
                found = {"name": name, "price": low, "lots": _count(offers.get("offerCount")), "id": None,
                         "slug": None, "keys": (name_key, "offers.lowPrice", "offers.offerCount")}
        if found:
            if same_name(name) != parent:
                yield found
            parent = same_name(name)
        for key, value in data.items():
            if not (found and key in LOT_KEYS):
                yield from walk_products(value, depth + 1, parent)
    elif isinstance(data, list):
        for value in data:
            yield from walk_products(value, depth + 1, parent)


# --- текст карточки и страница товара ---------------------------------------------

# «от» — отдельным словом: не «лот 150 ₽».
PRICE_FROM_RE = re.compile(r"(?<![А-Яа-яЁёA-Za-z])от\s*(\d{1,3}(?:[   ,.]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s*(?:₽|руб)", re.I)
PRICE_RE = re.compile(r"(\d{1,3}(?:[   ,.]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s*(?:₽|руб)", re.I)
LOTS_RE = re.compile(r"(?<![\w.,])(\d{1,3}(?:[   ]\d{3})+|\d+)[  ]*лот|лот(?:ов|а)?\s*:\s*(\d+)", re.I)
_BUTTON_RE = re.compile(r"^(купить|в корзину|продать|подробнее|от|лот\w*|шт\.?|new|hot|sale|скидка.*)$", re.I)
# Значки редкости и типа ("Ancient", "Knife", "MM2") — не название. "Chroma" — часть
# названия: значок Chroma над "Harvester" значит товар "Chroma Harvester".
_RARITY_RE = re.compile(r"^(common|uncommon|rare|legendary|godly|ancient|vintage|unique|pet|misc|knife|gun|weapon|"
                        r"mm2|мм2|нож|пистолет|оружие|питомец|"
                        r"обычн\w*|необычн\w*|редк\w*|легендарн\w*|древн\w*|винтажн\w*)$", re.I)
_CHROMA_RE = re.compile(r"^(?:chroma|хрома)$", re.I)


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
                 and not _BUTTON_RE.match(line) and not _RARITY_RE.match(line) and not _CHROMA_RE.match(line)
                 and clean_name(line)), None)
    if name and any(_CHROMA_RE.match(line) for line in lines) and not re.match(r"(?:chroma\b|c\.\s)", name, re.I):
        name = "Chroma " + name
    return {"name": name, "price": price, "lots": lots}


# --- карточки в разметке ------------------------------------------------------------

_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
              "track", "wbr"}
# Открытый тег, который браузер закрывает сам, когда начинается такой же (или блок — для <p>).
_IMPLIED_CLOSE = {"li": {"li"}, "p": {"p"}, "dt": {"dt", "dd"}, "dd": {"dt", "dd"}, "tr": {"tr"},
                  "td": {"td", "th"}, "th": {"td", "th"}, "option": {"option"}}
_CLOSES_P = {"address", "article", "aside", "blockquote", "div", "dl", "fieldset", "figure", "footer", "form",
             "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "main", "nav", "ol", "p", "pre", "section",
             "table", "ul"}
_TAG_RE = re.compile(r"<!--.*?-->|<(/?)([a-zA-Z][a-zA-Z0-9:-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>", re.S)
_HREF_RE = re.compile(r"""\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.I)
CARD_HREF_RE = re.compile(r"^(?:https?://[^/\"']*dreampets\.(?:gg|io))?(/mm2/product/[^\"'#?\s]+)", re.I)
_CARD_TOP = {None, "html", "body", "main"}
MAX_CARD = 50000  # символов разметки: одна ссылка на всю страницу — это уже не карточка
_HIDDEN_RE = re.compile(r"<(script|style|template|noscript|svg)\b.*?</\1\s*>", re.S | re.I)
_BLOCK_TAG_RE = re.compile(r"</?(?:address|article|aside|blockquote|br|button|dd|div|dl|dt|figcaption|figure|footer|"
                           r"form|h[1-6]|header|hr|img|label|li|main|nav|ol|option|p|section|select|summary|table|"
                           r"tbody|td|tfoot|th|thead|tr|ul)\b[^>]*>", re.I)


def card_fragments(page_html):
    """[(путь товара, разметка его карточки)] — для каждой ссылки на товар MM2.

    Карточка — самый большой элемент вокруг ссылки, где нет ссылок на другие товары
    (ссылки на тот же товар — картинка и название — и на разделы или продавцов не
    мешают). Так в карточку попадает всё: и цена до ссылки, и лоты после неё, но не
    цена соседней карточки."""
    # Скрипты и стили — пробелами той же длины: позиции тегов не сдвигаются.
    page_html = _HIDDEN_RE.sub(lambda m: " " * len(m.group(0)), page_html or "")
    root = {"tag": None, "start": 0, "end": len(page_html), "parent": None, "ids": set()}
    stack, anchors = [root], []
    for match in _TAG_RE.finditer(page_html):
        closing, tag, attrs = match.group(1), (match.group(2) or "").lower(), match.group(3) or ""
        if not tag:
            continue  # комментарий
        if closing:
            for depth in range(len(stack) - 1, 0, -1):
                if stack[depth]["tag"] == tag:
                    for node in stack[depth + 1:]:
                        node["end"] = match.start()
                    stack[depth]["end"] = match.end()
                    del stack[depth:]
                    break
            continue
        top = stack[-1]
        if top["tag"] in _IMPLIED_CLOSE.get(tag, ()) or (top["tag"] == "p" and tag in _CLOSES_P):
            top["end"] = match.start()
            stack.pop()
        if tag in _VOID_TAGS:
            continue
        node = {"tag": tag, "start": match.start(), "end": len(page_html), "parent": stack[-1], "ids": set()}
        stack.append(node)
        href = _HREF_RE.search(attrs) if tag == "a" else None
        path = CARD_HREF_RE.match(html_lib.unescape(next(g for g in href.groups() if g is not None))) if href else None
        if path:
            pid = uuid_of(path.group(1)) or path.group(1).lower().rstrip("/")
            anchors.append((node, path.group(1)))
            while node is not None:
                node["ids"].add(pid)
                node = node["parent"]
    cards = []
    for node, path in anchors:
        while (node["parent"]["tag"] not in _CARD_TOP and len(node["parent"]["ids"]) <= 1
               and node["parent"]["end"] - node["parent"]["start"] <= MAX_CARD):
            node = node["parent"]
        cards.append((path, page_html[node["start"]:node["end"]]))
    return cards


def visible_text(fragment):
    """Текст разметки по строкам. Соседние теги — разные надписи: "<i>114</i><b>230,71 ₽</b>"
    — это "114" и "230,71 ₽", а не "114 230,71 ₽"; "1<span>,</span>320" остаётся "1,320"."""
    text = _HIDDEN_RE.sub(" ", fragment or "")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r">(?=<)", ">\n", text)
    text = re.sub(r"<[^>]*>", "", _BLOCK_TAG_RE.sub("\n", text))
    lines = (re.sub(r"\s+", " ", html_lib.unescape(line)).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


_META_RE = re.compile(r"<meta\b[^>]*>", re.I)


def _meta(page_html, names):
    out = []
    for tag in _META_RE.findall(page_html):
        key = re.search(r"(?:name|property)\s*=\s*[\"']([^\"']+)[\"']", tag, re.I)
        content = re.search(r"content\s*=\s*(\"[^\"]*\"|'[^']*')", tag, re.I)
        if key and content and key.group(1).lower() in names:
            out.append(html_lib.unescape(content.group(1)[1:-1]))
    return out


# Конец основного блока страницы товара: дальше — другие товары со своими ценами.
_RELATED_RE = re.compile(r"^(?:похож|рекоменд|другие (?:товары|предметы|лоты)|с этим товаром|вам (?:может|также)|"
                         r"смотрите также|недавно просмотр|популярные|you may also|related|similar)", re.I)
_PROMO_RE = re.compile(r"бонус|пополн|кэшб|кешб|cashback|промокод|скидк|депозит|реферал", re.I)
_SOLD_RE = re.compile(r"продан|продаж|куплен", re.I)  # "Продано 1 250 лотов" — не лоты в продаже
_SOLD_OUT_RE = re.compile(r"нет в наличии|нет лотов|нет предложений|распродан|закончил|out of stock|sold out", re.I)


def _title_name(text):
    """"Купить Harvester в ММ2 — DreamPets", "Godseye | DreamPets" -> название товара."""
    text = html_lib.unescape(re.sub(r"\s+", " ", text or "")).strip()
    text = re.split(r"\s+(?:/|—|–|-)\s+|\s*\|\s*", text, maxsplit=1)[0]
    text = re.sub(r"^купить\s+", "", text.strip(), flags=re.I)
    text = re.sub(r"\s+(?:в|для)\s+(?:[MМ]{2}\s?2|murder mystery)\b.*$", "", text, flags=re.I)
    name = clean_name(text)
    return None if not name or re.search(r"dreampets", name, re.I) else name


def _page_name(page_html):
    title = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.S | re.I)
    title = title.group(1) if title else ""
    h1 = re.search(r"<h1\b[^>]*>(.*?)</h1>", page_html, re.S | re.I)
    candidates = [title] if "купить" in title.lower() else []
    candidates += _meta(page_html, ("og:title",))[:1]
    candidates += [visible_text(h1.group(1)).replace("\n", " ")] if h1 else []  # "Chroma" + "Harvester"
    return next((name for name in map(_title_name, candidates + [title]) if name), None)


def _page_link(page_html, url):
    """(uuid, slug) товара этой страницы — из её адреса, canonical или og:url."""
    links = [url] if url else []
    for tag in re.findall(r"<link\b[^>]*>", page_html, re.I):
        href = re.search(r"href\s*=\s*[\"']([^\"']+)", tag, re.I)
        if href and re.search(r"rel\s*=\s*[\"']?canonical", tag, re.I):
            links.append(html_lib.unescape(href.group(1)))
    links += _meta(page_html, ("og:url",))
    for link in links:
        if PRODUCT_PATH_RE.search(str(link)):
            slug = re.search(r"/mm2/product/([a-z0-9-]+)/" + UUID, str(link), re.I)
            return uuid_of(link), slug.group(1) if slug else None
    return None, None


def _page_product(page_html, name, uid, slug):
    """Этот товар в данных страницы: по uuid или slug, иначе по названию. Без названия
    чужой товар («похожие», «недавно смотрели») не берётся."""
    by_name = None
    for blob in embedded_json(page_html):
        for product in walk_products(blob):
            pid = uuid_of(str(product["id"])) if product["id"] is not None else None
            if uid and pid:
                if pid == uid:
                    return product
                continue  # другой товар
            if slug and product["slug"] and same_name(product["slug"]) == same_name(slug):
                return product
            if by_name is None and name and same_name(product["name"]) == same_name(name):
                by_name = product
    return by_name


def _main_lines(page_html):
    """Строки основного блока товара: от <h1> (или <main>) до «Похожие товары» и т. п.;
    шапка до заголовка, меню и подвал — не товар."""
    body = re.sub(r"<(head|nav|footer)\b.*?</\1\s*>", " ", _HIDDEN_RE.sub(" ", page_html),
                  flags=re.S | re.I)
    start = re.search(r"<h1\b", body, re.I) or re.search(r"<main\b", body, re.I)
    lines = visible_text(body[start.start():] if start else body).split("\n")
    end = next((number for number, line in enumerate(lines) if _RELATED_RE.match(line)), len(lines))
    return lines[:end]


def _scan(lines):
    """(цена «от N ₽», число лотов, «нет в наличии») в строках."""
    price = lots = None
    sold_out = False
    for line in lines:
        if price is None and not _PROMO_RE.search(line):
            match = PRICE_FROM_RE.search(line)
            price = rub(match.group(1)) if match else None
        if lots is None:
            lots = next((lots_value(m) for m in LOTS_RE.finditer(line) if not _SOLD_RE.search(line[:m.start()])),
                        None)
        sold_out = sold_out or bool(_SOLD_OUT_RE.search(line))
    return price, lots, sold_out


def parse_product_page(page_html, url=None):
    """{"name", "price", "lots", "source"} со страницы товара (url — её адрес, если известен).

    Сначала данные страницы — только этого товара (по uuid/slug или названию); лоты,
    которых там нет, — из описания страницы или основного блока. Без данных — описание
    и основной блок, но не баннеры, не «продано N» и не «Похожие товары». Лотов нет
    («Нет в наличии», «0 лотов») — price None, lots 0: товар не продаётся."""
    page_html = page_html or ""
    name = _page_name(page_html)
    product = _page_product(page_html, name, *_page_link(page_html, url))
    meta = _scan(_meta(page_html, ("description", "og:description", "twitter:description")))
    main = _scan(_main_lines(page_html))
    lots = next((found[1] for found in (meta, main) if found[1] is not None), None)
    if product:
        price, source = product["price"], "json"
        lots = product["lots"] if product["lots"] is not None else lots
    else:
        price, source = next((found[0] for found in (meta, main) if found[0] is not None), None), "text"
    if lots == 0 or (price is None and lots is None and (meta[2] or main[2])):
        price, lots = None, 0
    return {"name": name or (product["name"] if product else None), "price": price, "lots": lots, "source": source}


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
