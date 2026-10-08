"""Скриншоты инвентаря MM2: слова, найденные распознаванием, -> плитки (название, количество, место).

На вход — JSON скрипта распознавания (ocr._TILE_SCRIPT):
    {"width","height","words":[{"text","x","y","w","h","pass","line","cut","bg"}], ...}
Координаты — пиксели исходного скриншота.

Устройство плитки (измерено на плитке инвентаря при 1080p, ширина содержимого W = 130 px):
    значок «xN» — в правом верхнем углу: правый край = край плитки − 0.05 W, центр по y = верх + 0.15 W;
    название — по центру нижней полосы (баннера), центр по y = верх + 1.21 W;
    -> name.cy − badge.cy ≈ 1.06 W, badge.right − name.cx ≈ 0.45 W;
    шаг плиток ≈ 1.07 W, высота цифр значка ≈ 0.123 W. Нет значка — 1 штука.
"""

import difflib
import re
from collections import Counter

MARKER = "@@MM2OCR@@"

# --- geometry constants (fractions of the tile content width W) -------------------------
DY_NAME_BADGE = 1.06      # name centre y - badge centre y
DX_BADGE_NAME = 0.45      # badge right edge - name centre x
TILE_TOP = 1.21           # name centre y - tile top
TILE_BOTTOM = 0.10        # tile bottom - name centre y
W_PER_PITCH = 0.935       # tile width / horizontal pitch between neighbouring tiles
W_PER_BADGE_H = 8.1       # tile width / badge word height
W_PER_NAME_H = 7.0        # tile width / name word height (rough: names are auto-sized)

GAP_SPLIT = 0.8           # split an OCR line where the gap is > 0.8 x word height
NAME_MIN_SCORE = 0.72     # fuzzy score needed to call a phrase an item name
PASS_PRIORITY = ("white", "grayinv", "whitehi", "color")

# --- text normalisation ------------------------------------------------------------------
# Cyrillic look-alikes (a Russian OCR engine reads "Cowboy" as "\u0421\u043ewb\u043e\u0443"), x-sign, quotes, NBSP.
_HOMOGLYPHS = str.maketrans({
    "\u0410": "A", "\u0412": "B", "\u0415": "E", "\u041a": "K", "\u041c": "M", "\u041d": "H",
    "\u041e": "O", "\u0420": "P", "\u0421": "C", "\u0422": "T", "\u0425": "X", "\u0423": "Y",
    "\u0406": "I", "\u0408": "J", "\u0405": "S", "\u0401": "E", "\u0430": "a", "\u0432": "b",
    "\u0435": "e", "\u043a": "k", "\u043c": "m", "\u043d": "h", "\u043e": "o", "\u0440": "p",
    "\u0441": "c", "\u0442": "t", "\u0443": "y", "\u0445": "x", "\u0456": "i", "\u0458": "j",
    "\u0455": "s", "\u0451": "e", "\u044c": "b", "\u0501": "d", "\u051b": "q", "\u051d": "w",
    "\u00d7": "x", "\u2019": "'", "\u2018": "'", "\u00b4": "'", "`": "'", "\u00a0": " ",
    "\u2013": "-", "\u2014": "-",
})


def fold_text(text):
    """OCR text -> plain Latin: Cyrillic look-alikes from a Russian OCR engine, the x-sign, quotes."""
    return re.sub(r"\s+", " ", str(text or "").translate(_HOMOGLYPHS)).strip()


def name_key(name):
    """Same idea as parser.name_key: lowercase letters/digits only, "C. X" == "Chroma X"."""
    text = fold_text(name).lower()
    text = re.sub(r"^c\.\s*", "chroma ", text)
    return re.sub(r"[^a-z0-9]+", "", text)


# --- quantity badge ----------------------------------------------------------------------
_BADGE_RE = re.compile(r"^[(\[]?[xX*]\s?([0-9OoDQIl|!iSsZzBg]{1,4})[)\].,:;'\"]?$")
_TO_DIGIT = str.maketrans("OoDQIl|!iSsZzBg", "000011111552289")
_X_ONLY = {"x", "X", "*"}


def parse_badge(text):
    """'x40', 'X40', 'x 40', '\u00d740' (x-sign), 'x4O', 'xl0', '(x12)' -> int; anything else -> None.

    At least one real digit is required, unless the rest is only O/0-like and I/1-like
    letters ('xIO' -> 10). 'Xmas', 'Xbox', 'Xeno' are not badges.
    """
    match = _BADGE_RE.match(fold_text(text).replace(" ", ""))
    if not match:
        return None
    raw = match.group(1)
    if not re.search(r"\d", raw) and not re.fullmatch(r"[OoDQIl|!i]+", raw):
        return None
    if not re.search(r"\d", raw) and raw.lower() in ("i", "l"):
        return None
    value = int(raw.translate(_TO_DIGIT))
    return value if 1 <= value <= 9999 else None


def looks_like_banner(bg):
    """Rarity banners are saturated (yellow, pink, red...) or light; inventory UI text sits on dark grey/navy."""
    if not bg:
        return True
    return max(bg) - min(bg) >= 50 or max(bg) >= 140


def is_noise(text):
    """Punctuation, single stray characters from the weapon picture."""
    plain = re.sub(r"[^A-Za-z0-9]", "", fold_text(text))
    return len(plain) == 0


# --- boxes -------------------------------------------------------------------------------
def _right(o):
    return o["x"] + o["w"]


def _cx(o):
    return o["x"] + o["w"] / 2.0


def _cy(o):
    return o["y"] + o["h"] / 2.0


def _union(items):
    x0 = min(i["x"] for i in items)
    y0 = min(i["y"] for i in items)
    x1 = max(i["x"] + i["w"] for i in items)
    y1 = max(i["y"] + i["h"] for i in items)
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def _iou(a, b):
    ix = max(0, min(_right(a), _right(b)) - max(a["x"], b["x"]))
    iy = max(0, min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"]))
    inter = ix * iy
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / union if union > 0 else 0.0


def _same_place(a, b):
    """Two phrases (from different passes / crops) cover the same text."""
    if _iou(a, b) >= 0.3:
        return True
    h = min(a["h"], b["h"]) or 1
    overlap = min(_right(a), _right(b)) - max(a["x"], b["x"])
    return abs(_cy(a) - _cy(b)) < 0.5 * h and overlap >= 0.5 * min(a["w"], b["w"])


def _median(values, default=None):
    values = sorted(v for v in values if v)
    if not values:
        return default
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2.0


# --- words -> phrases (one OCR pass) -------------------------------------------------------
def _phrase(words, kind, qty=None):
    box = _union(words)
    box.update({
        "kind": kind,
        "text": " ".join(fold_text(w["text"]) for w in words),
        "words": list(words),
        "qty": qty,
        "pass": words[0].get("pass", ""),
        "cut": any(w.get("cut") for w in words),
        "bg": words[0].get("bg"),
    })
    if kind == "name" and len(words) > 1:
        box["bg"] = max((w.get("bg") or [0, 0, 0] for w in words), key=lambda c: max(c) - min(c))
    return box


def split_line(words, gap_k=GAP_SPLIT):
    """Words of one OCR line (sorted by x) -> runs; a new run starts at a big gap.

    Names of neighbouring tiles share a baseline, so Windows OCR returns one line
    'Cowboy Seer Ice Wing'; the gap between tiles is much wider than a space.
    """
    words = sorted(words, key=lambda w: w["x"])
    runs = []
    for word in words:
        if runs:
            prev = runs[-1][-1]
            h = max(prev["h"], word["h"])
            gap = word["x"] - _right(prev)
            jump = abs(_cy(word) - _cy(prev)) > 0.45 * h
            sizes = max(prev["h"], word["h"]) > 1.8 * max(1, min(prev["h"], word["h"]))
            if gap <= gap_k * h and not jump and not sizes:
                runs[-1].append(word)
                continue
        runs.append([word])
    return runs


def phrases_from_words(words):
    """Words of ONE pass -> (name phrases, badge phrases)."""
    lines = {}
    for word in words:
        lines.setdefault(word.get("line", ""), []).append(word)
    names, badges = [], []
    for line_words in lines.values():
        line_words = sorted(line_words, key=lambda w: w["x"])
        # 'x' '40' recognised as two words -> one badge word
        merged, skip = [], False
        for i, word in enumerate(line_words):
            if skip:
                skip = False
                continue
            nxt = line_words[i + 1] if i + 1 < len(line_words) else None
            if (nxt is not None and fold_text(word["text"]) in _X_ONLY
                    and re.fullmatch(r"\d{1,4}", fold_text(nxt["text"]))
                    and nxt["x"] - _right(word) <= 0.6 * max(word["h"], nxt["h"])):
                joined = dict(word)
                joined.update(_union([word, nxt]))
                joined["text"] = "x" + fold_text(nxt["text"])
                joined["cut"] = word.get("cut") or nxt.get("cut")
                merged.append(joined)
                skip = True
                continue
            merged.append(word)
        text_words = []
        for word in merged:
            qty = parse_badge(word["text"])
            if qty is not None:
                badges.append(_phrase([word], "badge", qty))
            elif not is_noise(word["text"]):
                text_words.append(word)
        for run in split_line(text_words):
            plain = "".join(fold_text(w["text"]) for w in run)
            letters = sum(c.isalpha() for c in plain)
            if letters >= 2 or sum(c.isalnum() for c in plain) >= 3:   # '2015', 'HL2', '8Bit'
                names.append(_phrase(run, "name"))
    return names, badges


# --- fuzzy item names --------------------------------------------------------------------
_CONFUSIONS = (("0", "o"), ("1", "l"), ("5", "s"), ("8", "b"), ("rn", "m"), ("vv", "w"), ("|", "l"))


class NameMatcher:
    """OCR phrase -> known item name. Names repeat across categories (Cowboy knife/gun),
    so the result is a display name; the caller maps it to items (picture decides)."""

    def __init__(self, names):
        self.by_key = {}
        for name in names:
            key = name_key(name)
            if len(key) >= 2:
                self.by_key.setdefault(key, name)
        self.keys = list(self.by_key)
        self._cache = {}

    def match(self, text):
        """-> (name or None, score 0..1, partial: bool)."""
        key = name_key(text)
        if key in self._cache:
            return self._cache[key]
        result = self._match(key)
        self._cache[key] = result
        return result

    def _match(self, key):
        if len(key) < 2:
            return None, 0.0, False
        if key in self.by_key:
            return self.by_key[key], 1.0, False
        variants = {key}
        for wrong, right in _CONFUSIONS:
            variants |= {v.replace(wrong, right) for v in variants}
        best, best_score, second = None, 0.0, 0.0
        for variant in variants:
            if variant in self.by_key:
                return self.by_key[variant], 0.97, False
            for cand in difflib.get_close_matches(variant, self.keys, n=3, cutoff=0.6):
                score = difflib.SequenceMatcher(None, variant, cand).ratio()
                if cand == best:
                    best_score = max(best_score, score)
                elif score > best_score:
                    best, best_score, second = cand, score, max(second, best_score)
                else:
                    second = max(second, score)
        need = 0.9 if len(key) <= 4 else (0.8 if len(key) <= 6 else NAME_MIN_SCORE)
        if best is not None and best_score >= need and best_score - second >= 0.04:
            return self.by_key[best], best_score, False
        # partly recognised name: a unique prefix/suffix/substring of one known name
        if len(key) >= 5:
            hits = {self.by_key[k] for k in self.keys if key in k}
            if len(hits) == 1:
                name = hits.pop()
                coverage = len(key) / float(len(name_key(name)))
                if coverage >= 0.5:
                    return name, 0.7 + 0.25 * coverage, True
        return None, best_score, False


def _split_by_names(phrase, matcher):
    """'Elderwood Scythe Corrupt' (two tiles glued) -> two phrases if both halves are names."""
    words = phrase["words"]
    whole = matcher.match(phrase["text"])
    if len(words) < 2 or whole[1] >= 0.9:
        return [phrase]
    best = None
    for i in range(1, len(words)):
        left = _phrase(words[:i], "name")
        right = _phrase(words[i:], "name")
        ls, rs = matcher.match(left["text"]), matcher.match(right["text"])
        if ls[0] and rs[0] and min(ls[1], rs[1]) > max(whole[1], NAME_MIN_SCORE):
            score = min(ls[1], rs[1])
            if best is None or score > best[0]:
                best = (score, left, right)
    if best is None:
        return [phrase]
    return _split_by_names(best[1], matcher) + _split_by_names(best[2], matcher)


def _split_wide(phrase, width):
    """A phrase wider than one tile is two names: split at the widest gap."""
    words = phrase["words"]
    if width is None or len(words) < 2 or phrase["w"] <= 1.02 * width:
        return [phrase]
    gaps = [(words[i]["x"] - _right(words[i - 1]), i) for i in range(1, len(words))]
    _, at = max(gaps)
    return (_split_wide(_phrase(words[:at], "name"), width)
            + _split_wide(_phrase(words[at:], "name"), width))


# --- across passes -----------------------------------------------------------------------
def _clusters(phrases):
    """Greedy clustering of the same text seen by several passes / overlapping crops."""
    groups = []
    for phrase in sorted(phrases, key=lambda p: (p["cut"], -p["w"])):
        for group in groups:
            if any(_same_place(phrase, other) for other in group):
                group.append(phrase)
                break
        else:
            groups.append([phrase])
    return groups


def _pass_rank(name):
    base = str(name).split("@")[0]
    return PASS_PRIORITY.index(base) if base in PASS_PRIORITY else len(PASS_PRIORITY)


def merge_names(phrases, matcher):
    """-> [{name, text, score, partial, votes, box..., bg}] one per place on the screen."""
    result = []
    for group in _clusters(phrases):
        whole = [p for p in group if not p["cut"]] or group
        scored = []
        for p in whole:
            name, score, partial = matcher.match(p["text"])
            scored.append((name, score, partial, p))
        votes = Counter(s[0] for s in scored if s[0])
        best = max(scored, key=lambda s: (s[0] is not None, votes.get(s[0], 0) if s[0] else 0,
                                          s[1], -_pass_rank(s[3]["pass"])))
        name, score, partial, p = best
        entry = {k: p[k] for k in ("x", "y", "w", "h", "text", "bg")}
        entry.update({"name": name if score >= NAME_MIN_SCORE else None, "score": round(score, 3),
                      "partial": partial, "votes": votes.get(name, 0) if name else 0,
                      "passes": len(group)})
        result.append(entry)
    return result


def merge_badges(phrases):
    result = []
    for group in _clusters(phrases):
        whole = [p for p in group if not p["cut"]] or group
        votes = Counter(p["qty"] for p in whole)
        top = max(votes.values())
        qty = min((p for p in whole if votes[p["qty"]] == top),
                  key=lambda p: (not re.fullmatch(r"x\d+", p["text"]), _pass_rank(p["pass"])))["qty"]
        box = _union([p for p in whole if p["qty"] == qty])
        box.update({"qty": qty, "votes": votes[qty], "passes": len(group)})
        result.append(box)
    return result


# --- geometry ----------------------------------------------------------------------------
def _rows(items, key=_cy):
    """Group boxes whose centres are on one horizontal line."""
    rows = []
    for item in sorted(items, key=key):
        if rows and abs(key(item) - key(rows[-1][-1])) <= 0.5 * max(item["h"], rows[-1][-1]["h"]):
            rows[-1].append(item)
        else:
            rows.append([item])
    return rows


def estimate_tile_width(names, badges):
    """Tile content width W in screenshot pixels, or None if nothing to go on."""
    pitches = []
    for row in _rows(badges, key=lambda b: b["y"]) + _rows(names):
        xs = sorted(_right(b) if "qty" in b else _cx(b) for b in row)
        pitches += [b - a for a, b in zip(xs, xs[1:]) if b - a > 0]
    if pitches:
        smallest = min(pitches)
        # neighbours are 1 pitch apart; a missing tile in between gives 2 pitches
        close = [p for p in pitches if p <= 1.5 * smallest]
        candidate = _median(close) * W_PER_PITCH
        # sanity check against text size: the tile is far wider than its badge text
        badge_h = _median([b["h"] for b in badges])
        if badge_h is None or candidate >= 4 * badge_h:
            return candidate
    badge_h = _median([b["h"] for b in badges])
    if badge_h:
        return badge_h * W_PER_BADGE_H
    name_h = _median([n["h"] for n in names])
    return name_h * W_PER_NAME_H if name_h else None


def associate(names, badges, width):
    """Pair every name with the badge of its own tile (badge is above-right of the name).

    -> [(name or None, badge or None)]; a name without a badge has quantity 1.
    """
    pairs = []
    for bi, badge in enumerate(badges):
        for ni, name in enumerate(names):
            dx = _right(badge) - _cx(name)
            dy = _cy(name) - _cy(badge)
            if not (0.15 * width <= dx <= 0.80 * width and 0.70 * width <= dy <= 1.45 * width):
                continue
            cost = ((dx - DX_BADGE_NAME * width) / width) ** 2 + ((dy - DY_NAME_BADGE * width) / width) ** 2
            pairs.append((cost, ni, bi))
    used_n, used_b, result = set(), set(), []
    for cost, ni, bi in sorted(pairs):
        if ni in used_n or bi in used_b:
            continue
        used_n.add(ni)
        used_b.add(bi)
        result.append((names[ni], badges[bi]))
    result += [(n, None) for i, n in enumerate(names) if i not in used_n]
    result += [(None, b) for i, b in enumerate(badges) if i not in used_b]
    return result


def tile_box(name, badge, width):
    """Approximate tile rectangle (x, y, w, h) for the picture matcher."""
    if width is None:
        return None
    if name is not None:
        cx, cy = _cx(name), _cy(name)
    else:
        cx, cy = _right(badge) - DX_BADGE_NAME * width, _cy(badge) + DY_NAME_BADGE * width
    x0, y0 = cx - width / 2.0, cy - TILE_TOP * width
    return [int(round(x0)), int(round(y0)), int(round(width)), int(round((TILE_TOP + TILE_BOTTOM) * width))]


# --- whole pipeline ----------------------------------------------------------------------
def recognize(ocr, known_names, matcher=None):
    """OCR JSON (dict) + known item names -> list of tiles, top-to-bottom, left-to-right:
    {"name", "text", "score", "partial", "qty", "qty_from_badge", "box", "name_box", "badge_box", "bg",
     "clipped", "qty_uncertain"}
    "name" is None when the text was not recognised or was cut by the screenshot border
    (the picture matcher can still identify the tile from "box").
    """
    matcher = matcher or NameMatcher(known_names)
    by_pass = {}
    for word in ocr.get("words", []):
        if not str(word.get("text", "")).strip() or word.get("w", 0) <= 0 or word.get("h", 0) <= 0:
            continue
        by_pass.setdefault(word.get("pass", ""), []).append(word)
    name_phrases, badge_phrases = [], []
    for words in by_pass.values():
        names, badges = phrases_from_words(words)
        name_phrases += names
        badge_phrases += badges
    badges = merge_badges(badge_phrases)
    width = estimate_tile_width([], badges) if badges else None
    refined = []
    for phrase in name_phrases:
        for part in _split_wide(phrase, width):
            refined += _split_by_names(part, matcher)
    names = merge_names(refined, matcher)
    # Keep phrases that look like names; drop picture noise that matches nothing.
    names = [n for n in names if n["name"] or (n["passes"] >= 2 and len(name_key(n["text"])) >= 3)]
    width = estimate_tile_width([n for n in names if n["name"]], badges) or width
    img_w, img_h = ocr.get("width"), ocr.get("height")

    def touches_border(box, margin=2):
        if box is None or not img_w or not img_h:
            return False
        return (box["x"] <= margin or box["y"] <= margin or _right(box) >= img_w - margin
                or box["y"] + box["h"] >= img_h - margin)

    def outside(box):
        """Estimated tile crosses the left/right border: the visible name may be only part of it."""
        if box is None or not img_w or not width:
            return False
        return box[0] < -0.1 * width or box[0] + box[2] > img_w + 0.1 * width

    tiles = []
    for name, badge in associate(names, badges, width) if width else [(n, None) for n in names]:
        box = tile_box(name, badge, width)
        clipped = touches_border(name) or outside(box)
        if clipped and badge is None:
            continue  # half a tile at the screenshot border: the other screenshot has it whole
        if clipped:
            # 'Chroma Seer' cut by the border reads 'Seer' - a real but different item
            name = dict(name, name=None)
        if name is not None and badge is None:
            if name["name"] is None:
                continue  # unrecognised text with no badge: not evidence of a tile
            if not looks_like_banner(name["bg"]) and not (name["score"] >= 0.95 and len(name_key(name["name"])) >= 5):
                continue  # UI label on a dark panel ('Back' ~ 'Black'), not a tile banner
        tiles.append({
            "name": name["name"] if name else None,
            "text": name["text"] if name else "",
            "score": name["score"] if name else 0.0,
            "partial": bool(name and name["partial"]),
            "qty": badge["qty"] if badge else 1,
            "qty_from_badge": badge is not None,
            "box": box,
            "name_box": [name[k] for k in ("x", "y", "w", "h")] if name else None,
            "badge_box": [badge[k] for k in ("x", "y", "w", "h")] if badge else None,
            "bg": name["bg"] if name else None,
            "clipped": clipped or touches_border(badge),
            # badge area above the screenshot: 'no badge' does not prove quantity 1
            "qty_uncertain": badge is None and box is not None and box[1] < -0.05 * width,
        })
    tiles = _dedupe_tiles(tiles)
    boxes = [dict(zip("xywh", t["box"] or t["name_box"]), tile=t) for t in tiles]
    ordered = []
    for row in _rows(boxes, key=lambda b: b["y"] + b["h"]):
        ordered += [b["tile"] for b in sorted(row, key=lambda b: b["x"])]
    return ordered


def _dedupe_tiles(tiles):
    """The same tile found twice (e.g. a long name split differently by two passes):
    keep the better name, and the badge quantity if either copy has one."""
    out = []
    for tile in tiles:
        twin = next((t for t in out if t["box"] and tile["box"]
                     and _iou(dict(zip("xywh", t["box"])), dict(zip("xywh", tile["box"]))) > 0.6), None)
        if twin is None:
            out.append(tile)
            continue
        best = max((tile, twin), key=lambda t: (t["name"] is not None, t["score"]))
        counted = max((tile, twin), key=lambda t: (t["qty_from_badge"], t["qty"]))
        merged = dict(best, qty=counted["qty"], qty_from_badge=counted["qty_from_badge"],
                      badge_box=counted["badge_box"], qty_uncertain=best["qty_uncertain"] and counted["qty_uncertain"])
        out[out.index(twin)] = merged
    return out


def tiles_to_lines(tiles):
    """For the existing text calculator: ['Cowboy x40', 'Seer']; unknown tiles are skipped."""
    return ["%s x%d" % (t["name"], t["qty"]) if t["qty"] > 1 else t["name"] for t in tiles if t["name"]]


def parse_output(stdout_bytes):
    """PowerShell stdout -> dict; tolerant to warnings printed before the JSON."""
    import json
    text = stdout_bytes.decode("utf-8-sig", "replace") if isinstance(stdout_bytes, bytes) else stdout_bytes
    at = text.rfind(MARKER)
    if at < 0:
        raise ValueError("no OCR result in output")
    return json.loads(text[at + len(MARKER):].strip().splitlines()[0])
