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
DEBUG_DIR = DATA_DIR / "debug"

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
    base["items"] = [
        item for item in base["items"]
        if isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("category"), str)
    ]
    if not isinstance(base.get("categories"), dict):
        base["categories"] = {}
    if not isinstance(base.get("errors"), list):
        base["errors"] = []
    return base


def _load_for_write(path):
    """Как load(), но повреждённый файл откладывается в сторону, и работа идёт с нуля."""
    path = Path(path or DATA_FILE)
    try:
        return load(path)
    except (ValueError, OSError):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            os.replace(path, path.with_name(f"values.broken-{stamp}.json"))
        except OSError:
            pass
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


def _save_debug(slug, page_html):
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        (DEBUG_DIR / f"{slug}.html").write_text(page_html, encoding="utf-8")
    except OSError:
        pass


def _check_parsed(slug, items, orphans, old_count):
    """Причина не доверять результату разбора или None."""
    if not items:
        return "предметы не найдены"
    if orphans > max(2, len(items) * MAX_ORPHAN_SHARE):
        return f"у {orphans} карточек не удалось определить название"
    if old_count >= 10 and len(items) < old_count * MIN_KEEP_SHARE:
        return f"найдено только {len(items)} предметов, а было {old_count}"
    return None


def update_from_site(fetch=None, log=print, path=None):
    """Скачать все категории MM2 и сохранить. Категории с ошибкой не трогаются."""
    own_fetcher = fetch is None
    fetch = fetch or fetcher.Fetcher()
    with _LOCK:
        old = _load_for_write(path)
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
            problem = _check_parsed(slug, items, orphans, old_counts.get(slug, 0))
            if problem:
                _save_debug(slug, page_html)
                message = f"{problem}; старые цены оставлены, страница сохранена в data/debug/{slug}.html"
                errors.append(_error(slug, message))
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
        data = _load_for_write(path)
        for slug, items in fetched:
            replace_category(data, slug, items, "site")
        failed = {error["category"] for error in errors}
        data["errors"] = [e for e in data.get("errors", []) if e.get("category") not in failed] + errors
        if site_last_updated:
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
    items = parser.items_from_text(text, category)
    last_updated = parser.find_last_updated(text)
    if not items and not last_updated:
        raise ValueError("В тексте не найдено ни одного предмета со значением (Value).")
    with _LOCK:
        data = _load_for_write(path)
        if items:
            replace_category(data, category, items, "import")
            data["fetched_at"] = now_iso()
        if last_updated:
            data["site_last_updated"] = last_updated
        save(data, path)
    return items, last_updated
