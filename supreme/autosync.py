"""Автообновление: программа сама следит за сайтом и подтягивает новые цены.

Раз в несколько минут загружается одна страница сайта и сравнивается дата
"Values Last Updated". Если на сайте цены обновились (или в программе цен ещё
нет, или они давно не обновлялись), запускается полное обновление.
"""

import threading
from datetime import datetime, timedelta, timezone

from . import fetcher, parser, store

CHECK_EVERY = 5 * 60          # секунд между проверками сайта
FIRST_CHECK_AFTER = 15        # первая проверка вскоре после запуска
MAX_AGE = 12 * 60 * 60        # даже без новой даты обновлять раз в 12 часов


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse_iso(text):
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def site_last_updated(fetch=None):
    """Дата обновления цен на сайте (как написано на сайте) или None."""
    own = fetch is None
    fetch = fetch or fetcher.Fetcher()
    try:
        return parser.find_last_updated(parser.html_to_text(fetch(fetcher.HOME_URL)))
    finally:
        if own:
            fetch.close()


class AutoSync:
    def __init__(self, job, data_path=None, check_site=site_last_updated,
                 interval=CHECK_EVERY, first_after=FIRST_CHECK_AFTER, max_age=MAX_AGE):
        self.job = job
        self.data_path = data_path
        self.check_site = check_site
        self.interval = interval
        self.max_age = max_age
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.enabled = True
        self.next_check_at = _now() + timedelta(seconds=first_after)
        self.last_check_at = None
        self.last_result = "ещё не проверялось"

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def check_now(self):
        with self._lock:
            self.next_check_at = _now()
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            with self._lock:
                wait = (self.next_check_at - _now()).total_seconds()
            if wait > 0:
                self._wake.wait(timeout=min(wait, 30))
                self._wake.clear()
                continue
            self._tick()
            with self._lock:
                self.next_check_at = _now() + timedelta(seconds=self.interval)

    def _decide(self):
        """Нужно ли полное обновление и почему (текст для окна программы)."""
        try:
            data = store.load(self.data_path)
        except Exception:
            return True, "файл с ценами повреждён — загружаю заново"
        if not data["items"]:
            return True, "цен ещё нет — загружаю с сайта"
        fetched = _parse_iso(data.get("fetched_at"))
        if fetched is None or (_now() - fetched).total_seconds() > self.max_age:
            return True, "цены давно не обновлялись — обновляю"
        try:
            site_date = self.check_site()
        except Exception as error:
            return False, f"не удалось проверить сайт: {error}"
        if not site_date:
            return False, "на сайте не нашлась дата обновления"
        if site_date != data.get("site_last_updated"):
            return True, f"на сайте новые цены ({site_date}) — обновляю"
        return False, "на сайте без изменений"

    def _tick(self):
        if self.job.running:
            result = "идёт обновление"
        else:
            needed, result = self._decide()
            if needed and not self.job.start():
                result = "идёт обновление"
        with self._lock:
            self.last_check_at = _now()
            self.last_result = result

    def status(self):
        with self._lock:
            return {
                "enabled": self.enabled,
                "interval": self.interval,
                "last_check_at": self.last_check_at.isoformat() if self.last_check_at else None,
                "next_check_at": self.next_check_at.isoformat(),
                "last_result": self.last_result,
            }

