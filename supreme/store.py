"""Хранение цен (data/values.json) и обновление с сайта."""

import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import fetcher, parser, paths

DATA_DIR = paths.data_dir()
DATA_FILE = DATA_DIR / "values.json"

# Защита от испорченной загрузки: если в категории стало намного меньше предметов,
# чем было, или много карточек без названия — старые цены не трогаем.
MIN_KEEP_SHARE = 0.5
MAX_ORPHAN_SHARE = 0.1

_LOCK = threading.RLock()  # одна запись в файл цен за раз


class UpdateError(Exception):
    pass


def empty_data():
    return {
        "site_last_updated": None,
        "fetched_at": None,
        "source": fetcher.HOME_URL,
        "categories": {},
        "errors": [],
        "items": [],
    }


def load(path=None):
    """Прочитать цены. Повреждённый файл -> ValueError."""
    path = Path(path or DATA_FILE)
    if not path.exists():
        return empty_data()
    with open(path, encoding="utf-8-sig") as file:
        data = json.load(file)
    if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
        raise ValueError("неверная структура файла")
    base = empty_data()
    base.update(data)
    base["items"] = [_clean_item(item) for item in base["items"] if _is_item(item)]
    categories = base.get("categories")
    base["categories"] = {
        slug: info for slug, info in (categories.items() if isinstance(categories, dict) else ())
        if isinstance(info, dict)
    }
    errors = base.get("errors") if isinstance(base.get("errors"), list) else []
    base["errors"] = [e for e in (_clean_error(error) for error in errors) if e]
    for key in ("site_last_updated", "fetched_at"):
        if not isinstance(base.get(key), str):
            base[key] = None
    return base


def _is_item(item):
    return isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("category"), str)


def _number_or_none(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _clean_item(item):
    """Привести предмет к ожидаемым типам (файл мог быть изменён вручную)."""
    item = dict(item)
    item["value"] = _number_or_none(item.get("value"))
    for key in ("demand", "rarity"):
        item[key] = item[key] if isinstance(item.get(key), int) and not isinstance(item.get(key), bool) else None
    for key in ("value_text", "range_text", "stability", "change", "origin", "aliases"):
        if not isinstance(item.get(key), str):
            item[key] = ""
    return item


_SLUG_BY_TITLE = {title: slug for slug, title in parser.CATEGORIES}


def _clean_error(error):
    """Ошибка категории как словарь; строки "Title: message" из прошлой версии тоже."""
    if isinstance(error, dict) and isinstance(error.get("category"), str):
        cleaned = {
            "category": error["category"],
            "title": str(error.get("title") or parser.CATEGORY_TITLES.get(error["category"], error["category"])),
            "message": str(error.get("message") or ""),
        }
        if isinstance(error.get("rejected_count"), int):
            cleaned["rejected_count"] = error["rejected_count"]
        return cleaned
    if isinstance(error, str) and ":" in error:
        title, message = error.split(":", 1)
        slug = _SLUG_BY_TITLE.get(title.strip())
        if slug:
            return {"category": slug, "title": title.strip(), "message": message.strip()}
    return None


def _load_for_write(path):
    """Как load(), но повреждённый файл откладывается в сторону, и работа идёт с нуля.

    Ошибка чтения (файл занят, нет прав) не считается повреждением: она передаётся
    дальше, чтобы не записать пустой файл поверх нормальных цен.
    """
    path = Path(path or DATA_FILE)
    for attempt in range(5):
        try:
            return load(path)
        except PermissionError:  # Windows: файл на секунду занят антивирусом
            if attempt == 4:
                raise
            time.sleep(0.2)
        except ValueError:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            os.replace(path, path.with_name(f"values.broken-{stamp}.json"))
            return empty_data()


def save(data, path=None):
    path = Path(path or DATA_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".values-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=1)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp_name, path)
                break
            except PermissionError:  # Windows: файл на секунду занят антивирусом
                if attempt == 4:
                    raise
                time.sleep(0.2)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def replace_category(data, category, items, source):
    """Заменить предметы одной категории, остальные оставить как есть."""
    data["items"] = [item for item in data["items"] if item["category"] != category] + items
    data.setdefault("categories", {})[category] = {"updated_at": now_iso(), "count": len(items), "source": source}
    data["errors"] = [error for error in data.get("errors", []) if error.get("category") != category]
    return data


def _error(category, message):
    return {"category": category, "title": parser.CATEGORY_TITLES.get(category, category), "message": message}


def _save_debug(debug_dir, slug, page_html):
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / f"{slug}.html").write_text(page_html, encoding="utf-8")
    except OSError:
        pass


def _too_many_orphans(items, orphans):
    return orphans > max(2, len(items) * MAX_ORPHAN_SHARE)


def _check_parsed(items, orphans, old_count, old_info, old_error):
    """Причина не доверять результату разбора или None."""
    if not items:
        return "предметы не найдены"
    if _too_many_orphans(items, orphans):
        return f"у {orphans} карточек не удалось определить название"
    shrank = old_count >= 10 and len(items) < old_count * MIN_KEEP_SHARE
    # Резкое уменьшение подозрительно, но если прошлые цены были импортом или
    # сайт второй раз подряд даёт столько же — значит, так и есть.
    if shrank and old_info.get("source") != "import" and (old_error or {}).get("rejected_count") != len(items):
        return f"найдено только {len(items)} предметов, а было {old_count}"
    return None


def update_from_site(fetch=None, log=print, path=None):
    """Скачать все категории MM2 и сохранить. Категории с ошибкой не трогаются."""
    own_fetcher = fetch is None
    fetch = fetch or fetcher.Fetcher()
    debug_dir = Path(path or DATA_FILE).parent / "debug"
    with _LOCK:
        old = _load_for_write(path)
    old_categories = {slug: dict(info) for slug, info in old["categories"].items()}
    old_errors = {error["category"]: error for error in old["errors"]}
    old_date = old.get("site_last_updated")
    old_counts = {}
    for item in old["items"]:
        old_counts[item["category"]] = old_counts.get(item["category"], 0) + 1

    fetched = []
    errors = []
    site_last_updated = None
    network_failures = 0
    try:
        for slug, title in parser.CATEGORIES:
            if network_failures >= 2 and not fetched:
                errors.append(_error(slug, "пропущено: нет соединения с сайтом"))
                continue
            log(f"{title}: загрузка…")
            try:
                page_html = fetch(fetcher.BASE_URL + slug)
                items, last_updated, orphans = parser.parse_category_page(page_html, slug)
            except fetcher.NetworkError as error:
                network_failures += 1
                errors.append(_error(slug, str(error)))
                log(f"{title}: ошибка — {error}")
                continue
            except Exception as error:  # одна плохая страница не должна сорвать остальные
                errors.append(_error(slug, str(error) or type(error).__name__))
                log(f"{title}: ошибка — {error}")
                continue
            network_failures = 0
            site_last_updated = site_last_updated or last_updated
            problem = _check_parsed(
                items, orphans, old_counts.get(slug, 0), old_categories.get(slug, {}), old_errors.get(slug)
            )
            if problem:
                _save_debug(debug_dir, slug, page_html)
                message = (
                    f"{problem}; старые цены оставлены, страница сохранена в {debug_dir / (slug + '.html')}. "
                    "Можно загрузить цены этой категории через «Импорт»."
                )
                error = _error(slug, message)
                error["rejected_count"] = len(items)
                errors.append(error)
                log(f"{title}: {message}")
                continue
            fetched.append((slug, items))
            log(f"{title}: {len(items)} шт.")
        if site_last_updated is None and fetched:
            try:
                home_text = parser.html_to_text(fetch(fetcher.HOME_URL))
                site_last_updated = parser.find_last_updated(home_text)
            except Exception as error:
                log(f"Дата обновления сайта не получена: {error}")
    finally:
        if own_fetcher:
            fetch.close()

    if not fetched:
        details = "\n".join(f"{e['title']}: {e['message']}" for e in errors)
        raise UpdateError(
            "Не удалось загрузить ни одной категории.\n" + details
            + "\nПеренесите цены через вкладку «Импорт» (Ctrl+A, Ctrl+C на странице сайта)."
        )

    with _LOCK:
        # Перечитать файл: пока шла загрузка, цены могли импортировать вручную.
        # Такие категории (и дату сайта из импорта) не перезаписываем.
        data = _load_for_write(path)
        touched = {slug for slug in parser.CATEGORY_TITLES if data["categories"].get(slug) != old_categories.get(slug)}
        fetched = [(slug, items) for slug, items in fetched if slug not in touched]
        errors = [error for error in errors if error["category"] not in touched]
        for slug, items in fetched:
            replace_category(data, slug, items, "site")
        failed = {error["category"] for error in errors}
        data["errors"] = [e for e in data["errors"] if e["category"] not in failed] + errors
        if site_last_updated and data.get("site_last_updated") == old_date:
            data["site_last_updated"] = site_last_updated
        data["fetched_at"] = now_iso()
        save(data, path)
    log(f"Готово: {len(data['items'])} предметов, обновлено категорий: {len(fetched)} из {len(parser.CATEGORIES)}")
    for error in errors:
        log(f"Не обновлено — {error['title']}: {error['message']}")
    return data


def import_text(text, category, path=None):
    """Загрузить текст, скопированный со страницы категории (Ctrl+A, Ctrl+C).

    Возвращает (предметы, дата обновления сайта из текста или None).
    """
    if category not in parser.CATEGORY_TITLES:
        raise ValueError(f"Неизвестная категория: {category}")
    items, orphans = parser.parse_items(text, category)
    last_updated = parser.find_last_updated(text)
    if not items and not last_updated:
        raise ValueError("В тексте не найдено ни одного предмета со значением (Value).")
    if items and _too_many_orphans(items, orphans):
        raise ValueError(
            f"У {orphans} предметов не удалось определить название — цены не изменены. "
            "Скопируйте страницу целиком (Ctrl+A, Ctrl+C) и вставьте ещё раз."
        )
    with _LOCK:
        data = _load_for_write(path)
        if items:
            replace_category(data, category, items, "import")
            data["fetched_at"] = now_iso()
        if last_updated:
            data["site_last_updated"] = last_updated
        save(data, path)
    return items, last_updated
