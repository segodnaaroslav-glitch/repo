"""Калькулятор продажи: какие предметы перечислены в тексте или на скриншоте и
сколько за них дадут StarPets и DreamPets."""

import difflib
import re

from . import markets, parser

_QTY_RE = re.compile(
    r"^(?:(?P<pre>\d{1,4})\s*(?:x|х|×|шт\.?)?\s+|(?:x|х|×)\s*(?P<pre_x>\d{1,4})\s+)?(?P<name>.+?)"
    r"(?:\s*(?:[-–—:]|x|х|×)\s*(?P<post>\d{1,4})\s*(?:шт\.?)?|\s+(?P<count>\d{1,4})\s*шт\.?)?$",
    re.I,
)
_NOISE_LINE_RE = re.compile(r"^[\W\d_]*$")
CUTOFF = 0.82  # насколько похожим должно быть название при опечатках распознавания


def parse_lines(text):
    """Строки -> [(название, количество)]: "Harvester x2", "2 Harvester", "Harvester - 3"."""
    result = []
    for raw in str(text or "").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line or _NOISE_LINE_RE.match(line):
            continue
        match = _QTY_RE.match(line)
        name = match.group("name").strip() if match else line
        qty = int(match.group("pre") or match.group("pre_x") or match.group("post") or match.group("count") or 1) if match else 1
        result.append((name, max(1, qty)))
    return result


def build_matcher(items):
    """Поиск предмета по названию, в том числе по похожему (опечатки распознавания)."""
    usable = [i for i in items if not i.get("secret") and not parser.is_placeholder(i)]
    index = markets.build_index(usable)
    keys = {}
    for item in usable:
        keys.setdefault(parser.name_key(item["name"]), item)

    def find(name):
        item = markets.find_item(index, {"name": name})
        if item:
            return item, 1.0
        key = parser.name_key(name)
        if len(key) < 3:
            return None, 0.0
        close = difflib.get_close_matches(key, list(keys), n=1, cutoff=CUTOFF)
        if close:
            return keys[close[0]], difflib.SequenceMatcher(None, key, close[0]).ratio()
        return None, 0.0

    return find


def match_text(text, items):
    """Найденные предметы (с количеством) и строки, которые не удалось узнать."""
    find = build_matcher(items)
    found = {}
    unmatched = []
    for name, qty in parse_lines(text):
        item, score = find(name)
        if item is None:
            unmatched.append(name)
            continue
        key = markets.item_id(item)
        entry = found.setdefault(key, {"item": item, "qty": 0, "score": score, "seen": []})
        entry["qty"] += qty
        entry["score"] = min(entry["score"], score)
        entry["seen"].append(name)
    return list(found.values()), unmatched
