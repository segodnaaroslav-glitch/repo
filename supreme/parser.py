"""Разбор страниц MM2 с supremevalues.com.

Страница категории (https://supremevalues.com/mm2/<категория>) состоит из
карточек. В тексте карточка выглядит так:

    Celestial
    Value - 2,600
    Range - [2,550 - 2,650]
    Stability - Stable
    Demand - 3 Rarity - 5
    Change in Value - (+50) +2.0%
    Origin - Christmas 2024

Иногда на странице есть ещё JSON-объект ``var _svPopup = {...}`` с данными
предметов — он используется как запасной источник.

Значение предмета для программы — первое число диапазона ("1,320 - 1,340" ->
1320). Если диапазона нет, берётся число из Value. Если на сайте вместо числа
текст (например "x2 T1 Commons"), числового значения нет, а текст сохраняется
как есть.
"""

import html as html_lib
import json
import re
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

# (адрес категории на сайте, название в меню сайта)
CATEGORIES = [
    ("sets", "Sets"),
    ("uniques", "Uniques"),
    ("evos", "Evos"),
    ("ancients", "Ancients"),
    ("vintages", "Vintages"),
    ("chromas", "Chromas"),
    ("godlies", "Godlies"),
    ("legendaries", "Legendaries"),
    ("rares", "Rares"),
    ("uncommons", "Uncommons"),
    ("commons", "Commons"),
    ("pets", "Pets"),
    ("misc", "Misc. Items"),
    ("untradables", "Untradables"),
]
CATEGORY_TITLES = dict(CATEGORIES)
WEAPON_CATEGORIES = (
    "uniques", "evos", "ancients", "vintages", "chromas",
    "godlies", "legendaries", "rares", "uncommons", "commons",
)

STABILITIES = (
    "Overpaid For", "Underpaid For", "Doing Well", "Fluctuating", "Improving",
    "Declining", "Receding", "Peaking", "Rising", "Dropping", "Stable",
)

# Число как на сайте: "2,600", "0.5", "126K", "1.2M".
_NUMBER = r"\d[\d,]*(?:\.\d+)?\s*[KkMmBb]?"
_NUMBER_RE = re.compile(rf"^({_NUMBER})$")
_RANGE_RE = re.compile(rf"^\[?\s*({_NUMBER})\s*(?:[-–—]|to)\s*({_NUMBER})\s*\]?$", re.I)
_SINGLE_RANGE_RE = re.compile(rf"^\[?\s*({_NUMBER})\s*\]?$")
_SUFFIX = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}

_LABEL_RE = re.compile(
    r"(?<![A-Za-z])(Change in Value|Value|Range|Stability|Demand|Rarity|Origin|Aliases|Contains)"
    r"\s*[-–—:]\s*",
    re.I,
)
_LABEL_KEYS = {
    "change in value": "change",
    "value": "value",
    "range": "range",
    "stability": "stability",
    "demand": "demand",
    "rarity": "rarity",
    "origin": "origin",
    "aliases": "aliases",
    "contains": "contains",
}
# Надписи интерфейса сайта, которые не являются названиями предметов.
_FREE_TEXT_FIELDS = ("origin", "aliases")
_UI_JUNK_RE = re.compile(r"\bInv\.?\s*Controls\b", re.I)
# Значки на карточках (между названием и Value), которые не являются названием.
_BADGES = {"new", "hot", "trending", "updated", "limited", "sale", "rising", "dropping"}
_LAST_UPDATED_RE = re.compile(
    r"Values?\s+(?:were\s+)?Last\s+Updated\s*(?:[-–—:]|on)?\s*(.*?)\s*(?://|\||$)", re.I
)


def parse_number(text):
    """"2,600" -> 2600, "126K" -> 126000, "0.5" -> 0.5. Не число -> None."""
    if text is None:
        return None
    match = _NUMBER_RE.match(str(text).strip(" *~≈+"))
    if not match:
        return None
    token = match.group(1).replace(",", "").replace(" ", "")
    suffix = token[-1].lower() if token[-1].isalpha() else ""
    try:
        number = Decimal(token[:-1] if suffix else token) * _SUFFIX[suffix]
    except InvalidOperation:
        return None
    return int(number) if number == number.to_integral_value() else float(number)


def range_low(range_text):
    """Первое число диапазона: "[1,320 - 1,340]" -> 1320. Нет диапазона -> None."""
    if not range_text:
        return None
    text = str(range_text).strip().replace("−", "-")
    match = _RANGE_RE.match(text) or _SINGLE_RANGE_RE.match(text)
    if match:
        return parse_number(match.group(1))
    # Другие записи диапазона: "(1,320 - 1,340)", "~1,320 ~ 1,340", "[1,320 - 1,340]*".
    numbers = re.findall(_NUMBER, text)
    rest = re.sub(_NUMBER, "", text)
    if len(numbers) == 2 and re.fullmatch(r"[\s\[\](){}~≈*+\-–—]*(?:to)?[\s\[\](){}~≈*+\-–—]*", rest, re.I):
        return parse_number(numbers[0])
    return None


def program_value(value_text, range_text):
    """Значение для программы: первое число диапазона (из Range или из самого
    Value, если там записан диапазон), иначе число из Value."""
    for text in (range_text, value_text):
        low = range_low(text)
        if low is not None:
            return low
    return parse_number(value_text)


def name_key(name):
    """Ключ для сравнения названий: без регистра, "C. X" == "Chroma X"."""
    text = re.sub(r"\s+", " ", str(name)).strip().lower()
    text = re.sub(r"^c\.\s*", "chroma ", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def clean_name(name):
    text = _UI_JUNK_RE.sub(" ", str(name).replace("\ufeff", ""))
    text = re.sub(r"\s+", " ", text).strip(" -–—:|•")
    if text[:3].lower() == "c. ":
        text = "Chroma " + text[3:]
    return text


def _to_int(text):
    match = re.search(r"\d+", str(text))
    return int(match.group()) if match else None


# --- HTML -> текст ---------------------------------------------------------

_BLOCK_TAGS = {
    "a", "address", "article", "aside", "blockquote", "br", "button", "dd", "details", "div",
    "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "hr", "img", "label", "li", "main", "nav", "ol", "option",
    "p", "section", "select", "summary", "table", "tbody", "td", "tfoot", "th", "thead",
    "tr", "ul",
}
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "title"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0

    # Соседние строчные теги без пробела ("<span>Demand -</span><span>2</span>") не должны
    # склеивать слова, поэтому вместо них ставится пробел.

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self.skip_depth += 1
        else:
            self.parts.append("\n" if tag in _BLOCK_TAGS else " ")

    def handle_startendtag(self, tag, attrs):
        if tag not in _SKIP_TAGS:
            self.parts.append("\n" if tag in _BLOCK_TAGS else " ")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
        else:
            self.parts.append("\n" if tag in _BLOCK_TAGS else " ")

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)


def html_to_text(page_html):
    """Видимый текст страницы: блоки — отдельные строки."""
    extractor = _TextExtractor()
    extractor.feed(page_html)
    extractor.close()
    lines = (re.sub(r"[ \t\r\f\v\xa0]+", " ", line).strip() for line in "".join(extractor.parts).split("\n"))
    return "\n".join(line for line in lines if line)


# --- карточки предметов ----------------------------------------------------

def _value_fits(key, text):
    """Подходит ли строка как значение поля (иначе это, скорее всего, название)."""
    text = text.strip()
    if not text:
        return False
    if key == "value":
        return bool(re.match(r"^[\s*~≈+]*(\d|x\s*\d|priceless|n/?a|untrad|none|\?)", text, re.I))
    if key == "range":
        return bool(re.match(r"^[\s*~≈+(]*(\[|\d|n/?a|none)", text, re.I))
    if key in ("demand", "rarity"):
        return bool(re.match(r"^(\d|n/?a)", text, re.I))
    if key == "stability":
        return any(text.lower().startswith(s.lower()) for s in STABILITIES) or text.lower().startswith("n/a")
    if key == "change":
        return bool(re.match(r"^[(\[+\-−0-9]|n/?a", text, re.I))
    return True


def _split_line(line):
    """Строка -> [(None, текст до меток), (метка, значение), ...]."""
    matches = list(_LABEL_RE.finditer(line))
    if not matches:
        return [(None, line)]
    segments = []
    head = line[:matches[0].start()].strip()
    if head:
        segments.append((None, head))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
        key = _LABEL_KEYS[re.sub(r"\s+", " ", match.group(1)).lower()]
        segments.append((key, line[match.end():end].strip()))
    return segments


def _is_name_like(text):
    text = clean_name(text)
    if text.lower() in _BADGES:
        return False
    return 2 <= len(text) <= 80 and bool(re.search(r"[A-Za-z]", text))


def _pick_name(candidates, known_keys):
    if known_keys:
        for candidate in reversed(candidates):
            if name_key(candidate) in known_keys:
                return candidate
    return candidates[-1] if candidates else None


def _set_field(card, key, text):
    text = re.sub(r"\s+", " ", text).strip()
    if card is None or not text or key in card:
        return
    if key == "stability":
        for stability in STABILITIES:
            if text.lower().startswith(stability.lower()):
                text = stability
                break
    card[key] = text


def parse_cards(text, known_names=()):
    """Найти карточки предметов в тексте страницы (или в скопированном тексте).

    Возвращает (карточки, сколько меток Value остались без названия).
    """
    known_keys = {name_key(name) for name in known_names}
    cards = []
    orphans = 0
    card = None
    candidates = []
    name_before_contains = None
    pending = None    # метка, значение которой ожидается на следующей строке
    tentative = None  # (метка, текст): значение поля или название следующей карточки

    def commit_tentative():
        nonlocal tentative
        if tentative:
            key, value = tentative
            _set_field(card, key, value)
            if candidates and candidates[-1] == clean_name(value):
                candidates.pop()
            tentative = None

    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        for key, value in _split_line(line):
            if key is None:
                commit_tentative()
                if pending and not clean_name(value):
                    pending = None  # служебная надпись ("Inv. Controls") — не значение поля
                    continue
                if pending and _value_fits(pending, value):
                    if pending in _FREE_TEXT_FIELDS and _is_name_like(value):
                        # Пустое поле перед следующей карточкой выглядит так же, как поле
                        # со значением: решим, когда станет видно, идёт ли дальше Value.
                        tentative = (pending, value)
                        candidates.append(clean_name(value))
                    else:
                        _set_field(card, pending, value)
                    pending = None
                    continue
                pending = None
                if _is_name_like(value):
                    candidates.append(clean_name(value))
                continue

            if key in ("value", "contains") and tentative:
                tentative = None  # это было название следующей карточки
            commit_tentative()
            pending = None
            if key == "value":
                name = name_before_contains or _pick_name(candidates, known_keys)
                card = {"name": name} if name else None
                if card:
                    cards.append(card)
                else:
                    orphans += 1
                candidates = []
                name_before_contains = None
            elif key == "contains" and name_before_contains is None:
                name_before_contains = _pick_name(candidates, known_keys)

            if key == "contains":
                continue
            if value:
                _set_field(card, key, value)
            else:
                pending = key
    commit_tentative()
    return cards, orphans


# --- JSON _svPopup ---------------------------------------------------------

_POPUP_FIELDS = {
    "value": ("value", "val", "supreme"),
    "range": ("range", "rangedvalue", "valuerange", "ranged"),
    "demand": ("demand",),
    "rarity": ("rarity",),
    "stability": ("stability", "trend"),
    "change": ("change", "changeinvalue", "lastchange"),
    "origin": ("origin",),
    "aliases": ("aliases", "alias"),
}


def _plain(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        if len(value) == 2 and all(parse_number(_plain(v)) is not None for v in value):
            return f"{_plain(value[0])} - {_plain(value[1])}"
        return ", ".join(_plain(v) for v in value)
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", str(value)))
    return re.sub(r"\s+", " ", text).strip()


def parse_popup(page_html):
    """Данные из ``var _svPopup = {...}``: {название: {поле: текст}}."""
    match = re.search(r"_svPopup\s*=\s*", page_html)
    if not match:
        return {}
    try:
        data, _ = json.JSONDecoder().raw_decode(page_html, match.end())
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    result = {}
    for name, info in data.items():
        if not isinstance(info, dict):
            continue
        normalized = {re.sub(r"[^a-z]", "", str(k).lower()): v for k, v in info.items()}
        fields = {}
        for field, aliases in _POPUP_FIELDS.items():
            for alias in aliases:
                text = _plain(normalized.get(alias))
                if text:
                    fields[field] = text
                    break
        display = _plain(normalized.get("name")) or str(name)
        result[clean_name(display)] = fields
    return result


# --- предметы ---------------------------------------------------------------

def make_item(name, category, fields):
    value_text = fields.get("value") or ""
    range_text = fields.get("range") or ""
    if range_text.strip("[] ").lower() in ("n/a", "na", "none", ""):
        range_text = ""
    range_text = range_text.strip().strip("[]").strip()
    return {
        "name": clean_name(name),
        "category": category,
        "value": program_value(value_text, range_text),
        "value_text": value_text,
        "range_text": range_text,
        "demand": _to_int(fields["demand"]) if fields.get("demand") else None,
        "rarity": _to_int(fields["rarity"]) if fields.get("rarity") else None,
        "stability": fields.get("stability") or "",
        "change": fields.get("change") or "",
        "origin": fields.get("origin") or "",
        "aliases": fields.get("aliases") or "",
    }


def _dedupe(items):
    seen = {}
    result = []
    for item in items:
        key = name_key(item["name"])
        count = seen.get(key, 0)
        seen[key] = count + 1
        if count:
            item["name"] = f"{item['name']} ({count + 1})"
        result.append(item)
    return result


def parse_items(text, category, popup=None):
    """Текст страницы -> (предметы, сколько меток Value остались без названия)."""
    popup = popup or {}
    popup_by_key = {name_key(name): (name, fields) for name, fields in popup.items()}
    items = []
    cards, orphans = parse_cards(text, popup.keys())
    for card in cards:
        name = card.pop("name")
        known = popup_by_key.get(name_key(name))
        if known:
            name = known[0]
            for field, value in known[1].items():
                card.setdefault(field, value)
        items.append(make_item(name, category, card))
    return _dedupe(items), orphans


def items_from_text(text, category, popup=None):
    return parse_items(text, category, popup)[0]


def find_last_updated(text):
    """Дата "Values Last Updated - ..." со страницы сайта (как написано на сайте)."""
    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        match = _LAST_UPDATED_RE.search(line)
        if not match:
            continue
        found = match.group(1)
        following = iter(lines[index + 1:index + 5])
        if not re.search(r"\d", found):
            # Метка, тире и дата могут быть отдельными блоками.
            found = next((l for l in following if not re.fullmatch(r"[-–—:|/\s]*", l)), "")
            found = found.split("//")[0].split("|")[0]
        found = found.strip(" -–—:.")
        if re.search(r"\d", found) and ":" not in found:
            # Время отдельным блоком: "October 5th, 2026" + "at 12:49 PM".
            nxt = next(following, "")
            if re.match(r"at\s+\d", nxt, re.I):
                found = f"{found} {nxt.split('//')[0].strip()}"
        if re.search(r"\d", found):
            return found
    return None


def parse_category_page(page_html, category):
    """HTML страницы категории -> (предметы, дата обновления на сайте,
    сколько меток Value остались без названия)."""
    text = html_to_text(page_html)
    popup = parse_popup(page_html)
    items, orphans = parse_items(text, category, popup)
    if not items and popup:
        items = _dedupe([make_item(name, category, fields) for name, fields in popup.items()])
        orphans = 0
    return items, find_last_updated(text), orphans
