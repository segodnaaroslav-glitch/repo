"""Хранение цен (data/values.json) и обновление с сайта."""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import fetcher, parser, paths

DATA_DIR = paths.data_dir()
DATA_FILE = DATA_DIR / "values.json"
DEBUG_DIR = DATA_DIR / "debug"


class UpdateError(Exception):
    pass


def empty_data():
    return {"site_last_updated": None, "fetched_at": None, "source": fetcher.HOME_URL, "items": []}


def load(path=None):
    path = Path(path or DATA_FILE)
    if not path.exists():
        return empty_data()
    with open(path, encoding="utf-8") as file:
        data = json.load(file)
    base = empty_data()
    base.update(data)
    return base


def save(data, path=None):
    path = Path(path or DATA_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".values-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=1)
            file.write("\n")
        os.replace(tmp_name, path)
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
    return data


def update_from_site(fetch=None, log=print, path=None):
    """Скачать все категории MM2 и сохранить. Категории с ошибкой не трогаются."""
    own_fetcher = fetch is None
    fetch = fetch or fetcher.Fetcher()
    data = load(path)
    errors = []
    updated = 0
    site_last_updated = None
    try:
        for slug, title in parser.CATEGORIES:
            log(f"{title}: загрузка…")
            try:
                page_html = fetch(fetcher.BASE_URL + slug)
            except fetcher.FetchError as error:
                errors.append(f"{title}: {error}")
                log(f"{title}: ошибка — {error}")
                continue
            items, last_updated = parser.parse_category_page(page_html, slug)
            site_last_updated = site_last_updated or last_updated
            if not items:
                DEBUG_DIR.mkdir(parents=True, exist_ok=True)
                (DEBUG_DIR / f"{slug}.html").write_text(page_html, encoding="utf-8")
                errors.append(f"{title}: предметы не найдены (страница сохранена в data/debug/{slug}.html)")
                log(f"{title}: предметы не найдены, старые данные оставлены")
                continue
            replace_category(data, slug, items, "site")
            updated += 1
            log(f"{title}: {len(items)} шт.")
        if site_last_updated is None:
            try:
                home_text = parser.html_to_text(fetch(fetcher.HOME_URL))
                site_last_updated = parser.find_last_updated(home_text)
            except fetcher.FetchError as error:
                log(f"Дата обновления сайта не получена: {error}")
    finally:
        if own_fetcher:
            fetch.close()

    if not updated:
        raise UpdateError("Не удалось загрузить ни одной категории.\n" + "\n".join(errors))
    if site_last_updated:
        data["site_last_updated"] = site_last_updated
    data["fetched_at"] = now_iso()
    data["errors"] = errors
    save(data, path)
    log(f"Готово: {len(data['items'])} предметов, обновлено категорий: {updated} из {len(parser.CATEGORIES)}")
    return data


def import_text(text, category, path=None):
    """Загрузить текст, скопированный со страницы категории (Ctrl+A, Ctrl+C)."""
    if category not in parser.CATEGORY_TITLES:
        raise ValueError(f"Неизвестная категория: {category}")
    items = parser.items_from_text(text, category)
    if not items:
        raise ValueError("В тексте не найдено ни одного предмета со значением (Value).")
    data = load(path)
    replace_category(data, category, items, "import")
    last_updated = parser.find_last_updated(text)
    if last_updated:
        data["site_last_updated"] = last_updated
    data["fetched_at"] = now_iso()
    save(data, path)
    return items
