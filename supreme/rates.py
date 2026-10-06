"""Курс доллара к рублю с биржи Rapira (пара USDT/RUB).

Открытый метод биржи без ключа: GET https://api.rapira.net/open/market/rates
(лимит — 100 запросов в минуту; программа делает один запрос раз в 10 минут).
Последний полученный курс сохраняется, чтобы цены пересчитывались и без сети.
"""

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import fetcher, store

RAPIRA_URL = "https://api.rapira.net/open/market/rates"
REFRESH = 10 * 60
RETRY = 60


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def parse_rapira(data):
    """Курс USDT/RUB из ответа биржи (ищется пара в любом месте ответа)."""
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            symbol = str(node.get("symbol") or node.get("pair") or "").upper().replace("_", "/").replace("-", "/")
            if symbol == "USDT/RUB":
                for key in ("close", "last", "lastPrice", "price"):
                    if _number(node.get(key)):
                        return _number(node[key])
                ask, bid = _number(node.get("askPrice")), _number(node.get("bidPrice"))
                if ask and bid:
                    return round((ask + bid) / 2, 4)
                return ask or bid
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def fetch_rate(timeout=20):
    request = urllib.request.Request(RAPIRA_URL, headers={"Accept": "application/json",
                                                          "User-Agent": fetcher.HEADERS["User-Agent"]})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise fetcher.FetchError(f"биржа Rapira недоступна: {error}") from error
    rate = parse_rapira(data)
    if rate is None:
        raise fetcher.FetchError("в ответе Rapira нет пары USDT/RUB")
    return rate


class RateWatcher:
    def __init__(self, data_path=None, fetch=fetch_rate, refresh=REFRESH):
        self.path = Path(data_path or store.DATA_FILE).with_name("rate.json")
        self._fetch = fetch
        self.refresh = refresh
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.usd_rub = None
        self.updated_at = None
        self.error = None
        self._load()

    def _load(self):
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(saved, dict) and _number(saved.get("usd_rub")):
            self.usd_rub = _number(saved["usd_rub"])
            self.updated_at = saved.get("updated_at") if isinstance(saved.get("updated_at"), str) else None

    def update(self):
        try:
            rate = self._fetch()
        except Exception as error:
            with self._lock:
                self.error = str(error) or type(error).__name__
            return False
        with self._lock:
            self.usd_rub = rate
            self.updated_at = _now_iso()
            self.error = None
            text = json.dumps({"usd_rub": rate, "updated_at": self.updated_at})
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(text, encoding="utf-8")
        except OSError:
            pass
        return True

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            ok = self.update()
            self._stop.wait(self.refresh if ok else RETRY)

    def status(self):
        with self._lock:
            return {
                "usd_rub": self.usd_rub,
                "updated_at": self.updated_at,
                "source": "Rapira, USDT/RUB",
                "error": self.error,
            }
