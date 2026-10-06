"""Автообновление: программа сама следит за сайтом и подтягивает новые цены.

Раз в несколько минут загружается одна страница сайта и сравнивается дата
"Values Last Updated". Если на сайте цены обновились (или в программе цен ещё
нет, или они давно не обновлялись), запускается полное обновление.
"""

import threading
import time
from datetime import datetime, timedelta, timezone

from . import fetcher, parser, store

CHECK_EVERY = 5 * 60          # секунд между проверками сайта
FIRST_CHECK_AFTER = 15        # первая проверка вскоре после запуска
MAX_AGE = 12 * 60 * 60        # даже без новой даты обновлять раз в 12 часов
RETRY_FAILED = 15 * 60        # не все категории загрузились — повтор не раньше чем через 15 минут
MAX_FAILURE_PAUSE = 3 * 60 * 60
FETCHER_LIFETIME = 60 * 60    # раз в час снова пробовать загрузку без браузера


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse_iso(text):
    try:
        moment = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


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
    def __init__(self, job, data_path=None, check_site=None,
                 interval=CHECK_EVERY, first_after=FIRST_CHECK_AFTER, max_age=MAX_AGE):
        self.job = job
        self.data_path = data_path
        self.check_site = check_site or self._check_site
        self._fetch = None
        self._fetch_born = 0.0
        self.failures = 0             # подряд неудачных обновлений
        self.pause_until = None       # до этого времени не запускать обновление самому
        self._seen_finish = job.status().get("finished_at") if hasattr(job, "status") else None
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

    def _check_site(self):
        """Проверка даты на сайте одним и тем же загрузчиком: если сайт пускает только
        через браузер, загрузчик это помнит и не пробует напрямую каждый раз."""
        if self._fetch is None or time.time() - self._fetch_born > FETCHER_LIFETIME:
            if self._fetch is not None:
                self._fetch.close()
            self._fetch = fetcher.Fetcher()
            self._fetch_born = time.time()
        return site_last_updated(self._fetch)

    def _note_finished_job(self):
        """Учесть, чем кончилось последнее обновление: ошибки подряд — пауза длиннее."""
        if not hasattr(self.job, "status"):
            return
        status = self.job.status()
        if status.get("running") or status.get("finished_at") == self._seen_finish:
            return
        self._seen_finish = status.get("finished_at")
        if status.get("error"):
            self.failures += 1
            pause = min(MAX_FAILURE_PAUSE, self.interval * (2 ** self.failures))
            self.pause_until = _now() + timedelta(seconds=pause)
        else:
            self.failures = 0
            self.pause_until = None

    def _decide(self):
        """Нужно ли полное обновление и почему (текст для окна программы)."""
        try:
            data = store.load(self.data_path)
        except OSError:
            return False, "файл с ценами сейчас занят — проверю позже"
        except Exception:
            return True, "файл с ценами повреждён — загружаю заново"
        if self.pause_until and _now() < self.pause_until:
            return False, (
                f"обновление не удалось {self.failures} раз подряд — повтор после "
                f"{self.pause_until.astimezone().strftime('%H:%M')}"
            )
        if not data["items"]:
            return True, "цен ещё нет — загружаю с сайта"
        fetched = _parse_iso(data.get("fetched_at"))
        if fetched is None or (_now() - fetched).total_seconds() > self.max_age:
            return True, "цены давно не обновлялись — обновляю"
        if data.get("errors") and (_now() - fetched).total_seconds() > RETRY_FAILED:
            return True, "не все категории обновились — повторяю"
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
        try:
            self._note_finished_job()
            if self.job.running:
                result = "идёт обновление"
            else:
                needed, result = self._decide()
                if needed and not self.job.start():
                    result = "идёт обновление"
        except Exception as error:  # поток проверки не должен останавливаться
            result = f"ошибка проверки: {error}"
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

