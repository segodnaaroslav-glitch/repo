"""Калькулятор продажи: какие предметы перечислены в тексте или на скриншоте и
сколько за них дадут StarPets и DreamPets."""

import difflib
import re

from . import markets, parser

_X = r"[xх×]"
# Количество перед названием: "2 Harvester", "2x Harvester", "x2 Harvester", "2 шт Harvester".
_PRE_RE = re.compile(rf"^(?:(?P<n>\d{{1,4}})\s*(?:{_X}|шт\.?)?|{_X}\s*(?P<xn>\d{{1,4}}))\s+(?P<name>\S.*)$", re.I)
# Количество после названия: "Harvester x2", "Harvester (3)", "Seer (x10)", "Harvester [x2]",
# "Seer (10 шт)", "Harvester - 3", "Harvester: 3", "Harvester 2", "Harvester 2x", "Icebreaker 5 шт".
# Перед "x" нужен пробел: "Phoenix 2" — это Phoenix, а не "Phoeni" × 2.
_POST_RE = re.compile(
    rf"^(?P<name>.*?\S)\s*(?:"
    rf"[(\[]\s*(?:{_X}\s*)?(?P<br>\d{{1,4}})\s*(?:{_X}|шт\.?)?\s*[)\]]"
    rf"|(?<=\s){_X}\s*(?P<x>\d{{1,4}})"
    rf"|[-–—:]\s*(?P<sep>\d{{1,4}})\s*(?:шт\.?)?"
    rf"|(?<=\s)(?P<bare>\d{{1,4}})\s*(?:{_X}|шт\.?)?"
    rf")$",
    re.I,
)
# Строка только с количеством ("x2" — значок на картинке предмета в инвентаре).
_QTY_ONLY_RE = re.compile(rf"^(?:{_X}\s*(?P<a>\d{{1,4}})|(?P<b>\d{{1,4}})\s*{_X})$", re.I)
_NOISE_LINE_RE = re.compile(r"^[\W\d_]*$")
CUTOFF = 0.82  # насколько похожим должно быть название при опечатках распознавания


def _split_qty(line):
    """"Harvester x2" -> ("Harvester", 2); без количества -> (строка, 1)."""
    match = _PRE_RE.match(line)
    if match and re.search(r"[A-Za-zА-Яа-яЁё]", match.group("name")):
        return match.group("name").strip(), int(match.group("n") or match.group("xn"))
    match = _POST_RE.match(line)
    if match and re.search(r"[A-Za-zА-Яа-яЁё]", match.group("name")):
        qty = next(int(match.group(g)) for g in ("br", "x", "sep", "bare") if match.group(g))
        return match.group("name").strip(), qty
    return line, 1


def parse_lines(text):
    """Строки -> [(название, количество)]: "Harvester x2", "2 Harvester", "Harvester - 3".

    Строка, где только количество ("x2"), относится к следующему предмету
    (на скриншоте значок обычно над названием), а если его нет — к предыдущему.
    """
    return [(name, qty) for _, name, qty, _ in _entries(text)]


def _entries(text):
    """[(строка, название, количество, количество, если вся строка — название)]."""
    result = []
    pending = None
    for raw in str(text or "").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            continue
        only = _QTY_ONLY_RE.match(line)
        if only:
            pending = int(only.group("a") or only.group("b"))
            continue
        if _NOISE_LINE_RE.match(line):
            continue
        name, qty = _split_qty(line)
        whole_qty = pending or 1
        if pending is not None and qty == 1:
            qty = pending
        pending = None
        result.append([line, name, max(1, qty), max(1, whole_qty)])
    if pending is not None and result and result[-1][2] == 1:
        result[-1][2] = result[-1][3] = max(1, pending)
    return [tuple(entry) for entry in result]


def build_matcher(items):
    """Поиск предмета по названию, в том числе по похожему (опечатки распознавания)."""
    usable = [i for i in items if not i.get("secret") and not parser.is_placeholder(i)]
    index = markets.build_index(usable)
    keys = {}
    for item in usable:
        keys.setdefault(parser.name_key(item["name"]), item)

    def find(name, line=None):
        # Строка целиком — точное название ("Candy 2" — предмет, а не Candy × 2).
        if line is not None and parser.name_key(line) in keys:
            return keys[parser.name_key(line)], 1.0, True
        item = markets.find_item(index, {"name": name})
        if item:
            return item, 1.0, False
        key = parser.name_key(name)
        if len(key) < 3:
            return None, 0.0, False
        close = difflib.get_close_matches(key, list(keys), n=1, cutoff=CUTOFF)
        if close:
            return keys[close[0]], difflib.SequenceMatcher(None, key, close[0]).ratio(), False
        return None, 0.0, False

    return find


def match_text(text, items):
    """Найденные предметы (с количеством) и строки, которые не удалось узнать."""
    find = build_matcher(items)
    found = {}
    unmatched = []

    def add(item, qty, score, seen):
        key = markets.item_id(item)
        entry = found.setdefault(key, {"item": item, "qty": 0, "score": score, "seen": []})
        entry["qty"] += qty
        entry["score"] = min(entry["score"], score)
        entry["seen"].append(seen)

    for line, name, qty, whole_qty in _entries(text):
        item, score, whole = find(name, line)
        if whole:
            name, qty = line, whole_qty
        if item is not None and score < 1 and len(name.split()) != len(item["name"].split()):
            item = None  # похоже лишь отчасти: в строке, видимо, несколько названий
        if item is not None:
            add(item, qty, score, name)
            continue
        # Распознавание часто отдаёт целый ряд инвентаря одной строкой:
        # "Harvester x2 Icebreaker Seer" — ищем названия внутри строки.
        hits, rest = _scan_line(line, find)
        if not hits:
            unmatched.append(name)
            continue
        for hit_item, hit_qty, hit_score, seen in hits:
            add(hit_item, hit_qty if hit_qty > 1 or whole_qty == 1 else whole_qty, hit_score, seen)
        unmatched.extend(rest)
    return list(found.values()), unmatched


_QTY_TOKEN_RE = re.compile(rf"^(?:{_X}\s*(\d{{1,4}})|(\d{{1,4}})\s*{_X}?|[(\[]\s*{_X}?\s*(\d{{1,4}})\s*[)\]])$", re.I)
MAX_NAME_WORDS = 5


def _qty_token(word):
    match = _QTY_TOKEN_RE.match(word)
    return int(next(g for g in match.groups() if g)) if match else None


def _scan_line(line, find):
    """Несколько названий в одной строке -> ([(предмет, количество, похожесть, текст)], нераспознанное)."""
    words = line.split()
    hits, rest, leftover = [], [], []
    pending = None
    index = 0
    while index < len(words):
        qty = _qty_token(words[index])
        if qty is not None:
            if hits and hits[-1][1] == 1 and pending is None and not leftover:
                hits[-1] = (hits[-1][0], qty, hits[-1][2], hits[-1][3])  # "Harvester x2"
            else:
                pending = qty  # "x2 Harvester"
            index += 1
            continue
        item = None
        for size in range(min(MAX_NAME_WORDS, len(words) - index), 0, -1):
            chunk = " ".join(words[index:index + size])
            has_qty = any(_qty_token(word) is not None for word in words[index:index + size])
            item, score, whole = find(chunk, chunk)
            if item is None or (has_qty and not whole):
                # Число внутри — только если это точное название ("Candy 2"), иначе это количество.
                item = None
                continue
            if score < 1 and len(chunk.split()) != len(item["name"].split()):
                # Похожее, но другой длины: "Icebreaker Seer" — это не Icebreaker с опечаткой.
                item = None
                continue
            if size > 1 or len(parser.name_key(chunk)) >= 3:
                break
            item = None
        if item is None:
            leftover.append(words[index])
            index += 1
            continue
        if leftover:
            rest.append(" ".join(leftover))
            leftover = []
        hits.append((item, pending or 1, score, chunk))
        pending = None
        index += size
    if leftover:
        rest.append(" ".join(leftover))
    # Обрывки без букв (значки, цифры) не показываются как «не узнал».
    rest = [r for r in rest if len(re.sub(r"[^A-Za-zА-Яа-яЁё]", "", r)) >= 3]
    return hits, rest


# --- плитки инвентаря со скриншотов --------------------------------------------------

# Цвет полосы с названием = редкость предмета. Используется, только чтобы выбрать между
# предметами с одинаковым названием (Cowboy — ружьё и нож); остальное решает пользователь.
_BANNER_HUES = (
    (40, 70, ("vintages",)),          # жёлтый
    (275, 335, ("godlies",)),         # розовый, фиолетовый
    (335, 361, ("legendaries",)),     # красный
    (0, 15, ("legendaries",)),
    (185, 255, ("rares",)),           # синий
    (85, 165, ("uncommons",)),        # зелёный
)


def banner_categories(bg):
    """Цвет полосы [r, g, b] -> категории, которым он подходит (пусто — не знаем)."""
    if not bg or len(bg) != 3:
        return ()
    r, g, b = (max(0, min(255, int(c))) / 255.0 for c in bg)
    high, low = max(r, g, b), min(r, g, b)
    if high - low < 0.12:
        return ("commons",) if high > 0.55 else ()
    if high == r:
        hue = (60 * ((g - b) / (high - low))) % 360
    elif high == g:
        hue = 60 * ((b - r) / (high - low)) + 120
    else:
        hue = 60 * ((r - g) / (high - low)) + 240
    for start, end, categories in _BANNER_HUES:
        if start <= hue < end:
            return categories
    return ()


def merge_shots(shots):
    """Плитки с нескольких скриншотов одного инвентаря -> {название: [плитки]}.

    Одинаковые предметы в MM2 лежат одной стопкой, поэтому один и тот же предмет на двух
    скриншотах (пересекающихся или одинаковых) — это одна плитка: берётся наибольшее
    количество, а не сумма. Две плитки с одним названием на одном скриншоте (нож и ружьё
    Cowboy) остаются двумя.
    """
    merged = {}
    for tiles in shots:
        here = {}
        for tile in tiles:
            if tile.get("name"):
                here.setdefault(tile["name"], []).append(tile)
        for name, group in here.items():
            group.sort(key=lambda t: -int(t.get("qty") or 1))
            have = merged.setdefault(name, [])
            for index, tile in enumerate(group):
                if index >= len(have):
                    have.append(tile)
                elif int(tile.get("qty") or 1) > int(have[index].get("qty") or 1):
                    have[index] = tile
    return merged


def match_tiles(shots, items):
    """Плитки со скриншотов -> найденные предметы (как у match_text) и неузнанные названия.

    У записи есть "alternatives" — другие предметы с тем же названием, и "ambiguous", если
    выбрать по цвету полосы не удалось (пусть выберет пользователь).
    """
    usable = [i for i in items if not i.get("secret") and not parser.is_placeholder(i)]
    by_key = {}
    for item in usable:
        by_key.setdefault(parser.name_key(item["name"]), []).append(item)
    found, unmatched = {}, []
    for name, tiles in merge_shots(shots).items():
        candidates = by_key.get(parser.name_key(name)) or []
        if not candidates:
            unmatched.append(name)
            continue
        for tile in tiles:
            item, sure = candidates[0], len(candidates) == 1
            if not sure:
                fits = [c for c in candidates if c["category"] in banner_categories(tile.get("bg"))]
                if len(fits) == 1:
                    item, sure = fits[0], True
            key = markets.item_id(item)
            qty = max(1, int(tile.get("qty") or 1))
            entry = found.setdefault(key, {
                "item": item, "qty": 0, "score": float(tile.get("score") or 1.0), "seen": [],
                "alternatives": [c for c in candidates if c is not item], "ambiguous": False,
                "qty_uncertain": False, "from_screenshot": True,
            })
            entry["qty"] += qty
            entry["score"] = min(entry["score"], float(tile.get("score") or 1.0))
            entry["seen"].append(f"{tile.get('text') or name} ×{qty}" if qty > 1 else (tile.get("text") or name))
            entry["ambiguous"] = entry["ambiguous"] or not sure
            entry["qty_uncertain"] = entry["qty_uncertain"] or bool(tile.get("qty_uncertain"))
    return list(found.values()), unmatched
