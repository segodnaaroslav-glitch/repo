"""Значения оружия MM2 по сайту Supreme Values (https://supremevalues.com/).

Значение каждого оружия — первое число диапазона с сайта:
"1320 - 1340" -> 1320.

Запуск:
    python3 mm2_values.py              # показать всё оружие
    python3 mm2_values.py chroma       # поиск по названию
    python3 mm2_values.py --import values.txt   # загрузить значения из текста
"""

import json
import re
import sys
from pathlib import Path

DATA_FILE = Path(__file__).with_name("weapons.json")

# Число может содержать запятые как разделители тысяч: "1,320".
_NUMBER = r"\d[\d,]*(?:\.\d+)?"
_RANGE_LINE = re.compile(rf"^(?P<name>.+?)\s*[:\t]?\s*(?P<value>{_NUMBER}(?:\s*-\s*{_NUMBER})?)\s*$")


def parse_value(text):
    """Вернуть первое число из строки значения: "1320 - 1340" -> 1320."""
    match = re.search(_NUMBER, text)
    if not match:
        raise ValueError(f"В строке нет числа: {text!r}")
    number = match.group().replace(",", "")
    return float(number) if "." in number else int(number)


def load_weapons():
    if not DATA_FILE.exists():
        return {}
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


def save_weapons(weapons):
    DATA_FILE.write_text(
        json.dumps(weapons, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def import_text(path):
    """Прочитать строки вида "Название 1320 - 1340" и сохранить первое число."""
    weapons = load_weapons()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        match = _RANGE_LINE.match(line)
        if not match:
            print(f"Пропущена строка: {line}", file=sys.stderr)
            continue
        weapons[match.group("name").strip()] = parse_value(match.group("value"))
    save_weapons(weapons)
    return weapons


def main(args):
    if args[:1] == ["--import"]:
        weapons = import_text(args[1])
        print(f"Загружено оружия: {len(weapons)}")
        return

    weapons = load_weapons()
    query = " ".join(args).lower()
    found = {name: value for name, value in weapons.items() if query in name.lower()}
    if not found:
        print("Ничего не найдено." if weapons else "Список оружия пуст.")
        return
    for name, value in sorted(found.items(), key=lambda item: -item[1]):
        print(f"{name}: {value}")


if __name__ == "__main__":
    main(sys.argv[1:])
