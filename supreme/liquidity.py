"""Оценка ликвидности: насколько легко продать или обменять предмет по его значению.

Оценка строится на данных Supreme Values:
  * спрос (Demand) — главный показатель: сколько людей хотят предмет;
  * стабильность (Stability) — платят ли за предмет больше или меньше значения;
  * ширина диапазона (Range) — насколько трейдеры сходятся в цене.

Итог — балл 0–100 и уровень: ликвидный (>= 60), средний (>= 30), неликвидный.
"""

from . import parser

LIQUID = "liquid"
MEDIUM = "medium"
ILLIQUID = "illiquid"
UNTRADABLE = "untradable"

LIQUID_FROM = 60
MEDIUM_FROM = 30
DEMAND_SCALE = 10

_STABILITY = {
    "overpaid for": (15, "за него платят больше значения"),
    "doing well": (10, "цена растёт"),
    "improving": (8, "цена растёт"),
    "rising": (8, "цена растёт"),
    "stable": (0, "цена стабильная"),
    "peaking": (-3, "цена на пике, может пойти вниз"),
    "fluctuating": (-8, "цена скачет"),
    "underpaid for": (-12, "за него платят меньше значения"),
    "declining": (-12, "цена падает"),
    "dropping": (-15, "цена падает"),
    "receding": (-15, "цена падает"),
}


def _range_width(item):
    text = item.get("range_text") or ""
    match = parser._RANGE_RE.match(text.strip())
    if not match:
        return None
    low, high = parser.parse_number(match.group(1)), parser.parse_number(match.group(2))
    if not low or high is None or high < low:
        return None
    return (high - low) / low


def assess(item):
    """{"score": 0–100, "level": ..., "reasons": [...]} для одного предмета."""
    if item.get("category") == "untradables":
        return {"score": 0, "level": UNTRADABLE, "reasons": ["предмет нельзя обменять"]}

    reasons = []
    demand = item.get("demand")
    if demand is None:
        score = 0
        reasons.append("спрос не указан на сайте")
    else:
        demand = max(0, min(demand, DEMAND_SCALE))
        score = demand * 100 / DEMAND_SCALE
        word = "высокий" if demand >= 6 else "средний" if demand >= 3 else "низкий"
        reasons.append(f"спрос {demand}/{DEMAND_SCALE} — {word}")

    stability = (item.get("stability") or "").strip().lower()
    if stability in _STABILITY:
        bonus, text = _STABILITY[stability]
        score += bonus
        reasons.append(f"{item['stability']}: {text}")

    width = _range_width(item)
    if width is not None:
        if width <= 0.05:
            score += 5
            reasons.append("узкий диапазон цены — трейдеры согласны в цене")
        elif width > 0.2:
            score -= 5
            reasons.append("широкий диапазон цены — цену трудно определить")

    if item.get("value") is None:
        reasons.append("на сайте нет числового значения: " + (item.get("value_text") or "—"))

    score = int(round(max(0, min(100, score))))
    level = LIQUID if score >= LIQUID_FROM else MEDIUM if score >= MEDIUM_FROM else ILLIQUID
    return {"score": score, "level": level, "reasons": reasons}
