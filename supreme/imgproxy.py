"""Картинки предметов через свой сервер — для сравнения плиток со скриншотов по картинке.

Браузер может читать пиксели картинки (canvas) только со своего адреса: картинка
с supremevalues.com или cdn.starpets.gg «портит» canvas. Поэтому картинки предметов
отдаются с 127.0.0.1: сервер скачивает их сам, проверяет и хранит в папке с ценами
(data/images), чтобы скачивать один раз. Признаки картинок (их считает браузер,
web/picmatch.js) хранятся в data/picfeat.json — тоже считаются один раз.

Это не открытый прокси: адрес берётся только из данных известного предмета (по его
ключу), только https и только с supremevalues.com и starpets.gg.
"""

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ALLOWED_SITES = ("supremevalues.com", "starpets.gg")  # и их поддомены (cdn.starpets.gg)
MAX_BYTES = 2 * 1024 * 1024        # картинка предмета — десятки КБ; больше — не картинка предмета
MAX_CACHE_BYTES = 150 * 1024 * 1024
TIMEOUT = 10
FAIL_RETRY = 6 * 3600              # адрес, который ответил «нет такой картинки», не запрашивается 6 часов
BUSY_RETRY = 10 * 60               # сайт перегружен (429, 5xx) — 10 минут
OFFLINE_AFTER = 4                  # столько обрывов связи подряд — сети нет:
OFFLINE_PAUSE = 60                 # минуту картинки не запрашиваются (ответ сразу, без ожидания)
PARALLEL = 4                       # одновременных загрузок с сайтов
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
    "Accept": "image/webp,image/png,image/jpeg,image/*;q=0.8",
}
# Только растровые форматы: SVG может содержать скрипты, HTML — не картинка.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
)
_EXTENSIONS = (".webp", ".png", ".jpg", ".gif", ".avif")


class ImageError(Exception):
    """kind: "final" — картинки нет (запомнить надолго), "busy" — сайт занят,
    "network" — нет связи (не запоминать для адреса)."""

    def __init__(self, status, message, kind="final"):
        super().__init__(message)
        self.status = status
        self.kind = kind


def sniff(data):
    """Тип картинки по первым байтам: (content-type, расширение) или None."""
    for magic, ctype, ext in _MAGIC:
        if data.startswith(magic):
            return ctype, ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif", ".avif"
    return None


def host_allowed(url):
    """https, порт 443, без логина в адресе и только supremevalues.com / starpets.gg."""
    try:
        parts = urllib.parse.urlsplit(str(url))
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    return (parts.scheme == "https" and port in (None, 443) and parts.username is None and "@" not in parts.netloc
            and any(host == site or host.endswith("." + site) for site in ALLOWED_SITES))


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    """Перенаправление — только на те же доверенные сайты."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not host_allowed(newurl):
            raise urllib.error.HTTPError(newurl, code, "перенаправление на чужой сайт", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url, opener=None):
    """Скачать картинку: (байты, (content-type, расширение)). ImageError — если это не картинка."""
    opener = opener or urllib.request.build_opener(_CheckedRedirects)
    request = urllib.request.Request(url, headers=HEADERS)
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            declared = response.headers.get("Content-Length")
            if declared and declared.strip().isdigit() and int(declared) > MAX_BYTES:
                raise ImageError(502, "картинка слишком большая")
            ctype = (response.headers.get_content_type() or "").lower()
            if not (ctype.startswith("image/") or ctype in ("application/octet-stream", "binary/octet-stream")):
                raise ImageError(502, f"сайт вернул не картинку ({ctype})")
            body = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        busy = error.code == 429 or error.code >= 500
        status = 404 if error.code in (404, 410) else 503 if busy else 502
        raise ImageError(status, f"сайт картинок ответил HTTP {error.code}", "busy" if busy else "final") from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise ImageError(503, f"нет связи с сайтом картинок ({error})", "network") from None
    if len(body) > MAX_BYTES:
        raise ImageError(502, "картинка слишком большая")
    kind = sniff(body)
    if kind is None:
        raise ImageError(502, "сайт вернул не картинку")
    return body, kind


def _atomic_write(path, data):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ImageCache:
    """Картинки на диске: <папка>/<sha1 адреса>.<расширение>; неудачи — в failed.json."""

    def __init__(self, folder, download=_download, clock=time.time):
        self.folder = Path(folder)
        self._download = download
        self._clock = clock
        self._lock = threading.Lock()
        self._url_locks = {}
        self._slots = threading.BoundedSemaphore(PARALLEL)
        self._failed = None
        self._size = None
        self._network_errors = 0
        self._offline_until = 0.0

    @staticmethod
    def _name(url):
        return hashlib.sha1(url.encode("utf-8")).hexdigest()[:24]

    def _find(self, url):
        name = self._name(url)
        for ext in _EXTENSIONS:
            path = self.folder / (name + ext)
            if path.is_file():
                return path
        return None

    def _failures(self):
        if self._failed is None:
            try:
                failed = json.loads((self.folder / "failed.json").read_text("utf-8"))
                self._failed = {k: v for k, v in failed.items() if isinstance(v, list) and len(v) == 2}
            except (OSError, ValueError, AttributeError):
                self._failed = {}
        return self._failed

    def _remember_failure(self, url, kind):
        with self._lock:
            failed = self._failures()
            failed[url] = [self._clock(), kind]
            for key in sorted(failed, key=lambda k: failed[k][0])[:-2000]:
                failed.pop(key, None)
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                _atomic_write(self.folder / "failed.json", json.dumps(failed).encode("utf-8"))
            except OSError:
                pass

    def get(self, url):
        """(байты, content-type). ImageError — если картинку не получить."""
        if not host_allowed(url):
            raise ImageError(403, "картинки берутся только с supremevalues.com и starpets.gg")
        with self._lock:
            url_lock = self._url_locks.setdefault(url, threading.Lock())
        with url_lock:  # один адрес качается один раз, остальные запросы ждут файл
            path = self._find(url)
            if path is not None:
                try:
                    data = path.read_bytes()
                except OSError:
                    data = b""
                kind = sniff(data)
                if kind:
                    return data, kind[0]
            with self._lock:
                failed = self._failures().get(url)
                offline = self._clock() < self._offline_until
            if failed:
                at, kind = failed
                if self._clock() - at < (BUSY_RETRY if kind == "busy" else FAIL_RETRY):
                    raise ImageError(404 if kind == "final" else 503, "картинка недавно не загрузилась", kind)
            if offline:
                raise ImageError(503, "нет связи с сайтами картинок", "network")
            with self._slots:
                try:
                    data, (ctype, ext) = self._download(url)
                except ImageError as error:
                    self._after_error(url, error)
                    raise
            with self._lock:
                self._network_errors = 0
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                _atomic_write(self.folder / (self._name(url) + ext), data)
                self._added(len(data))
            except OSError:
                pass  # не сохранилась — отдадим и так
            return data, ctype

    def _after_error(self, url, error):
        if error.kind == "network":
            with self._lock:
                self._network_errors += 1
                if self._network_errors >= OFFLINE_AFTER:
                    self._offline_until = self._clock() + OFFLINE_PAUSE
                    self._network_errors = 0
            return  # сеть пропала — сам адрес не виноват
        self._remember_failure(url, error.kind)

    def _added(self, size):
        with self._lock:
            if self._size is None:
                self._size = sum(p.stat().st_size for p in self.folder.iterdir()
                                 if p.is_file() and p.suffix in _EXTENSIONS)
            else:
                self._size += size
            if self._size <= MAX_CACHE_BYTES:
                return
            files = [p for p in self.folder.iterdir() if p.is_file() and p.suffix in _EXTENSIONS]
            for path in sorted(files, key=lambda p: p.stat().st_mtime):
                if self._size <= MAX_CACHE_BYTES * 0.8:
                    break
                try:
                    size = path.stat().st_size
                    path.unlink()
                    self._size -= size
                except OSError:
                    pass


# --- признаки картинок (считает браузер, хранит сервер) ---------------------------

FEAT_VERSION = 1            # как PicMatch.FEAT_VERSION в web/picmatch.js
MAX_FEAT_CHARS = 4096       # одна упакованная запись PicMatch.pack (~2.2 тыс. символов)
MAX_LEARNED = 3             # картинок из игры на предмет (последние)
SOURCES = ("site", "market")
_BUCKET_RE = re.compile(r"grey|rainbow|\d{1,3}")
_FEAT_RE = re.compile(r"[A-Za-z0-9+/]+={0,2}")


def _feature_ok(value):
    return isinstance(value, str) and 0 < len(value) <= MAX_FEAT_CHARS and bool(_FEAT_RE.fullmatch(value))


class FeatureStore:
    """data/picfeat.json: {"version", "feats": {ключ: {"site"|"market": {"url", "f"}}},
    "learned": {ключ: [f, ...]}, "banners": {корзина цвета баннера: {категория: число}}}."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data = None

    def _load(self):
        if self._data is None:
            try:
                data = json.loads(self.path.read_text("utf-8"))
                if not isinstance(data, dict) or data.get("version") != FEAT_VERSION:
                    raise ValueError("другая версия")
                for field in ("feats", "learned", "banners"):
                    if not isinstance(data.get(field), dict):
                        data[field] = {}
            except (OSError, ValueError):
                data = {"version": FEAT_VERSION, "feats": {}, "learned": {}, "banners": {}}
            self._data = data
        return self._data

    def snapshot_json(self, extra=None):
        """Всё сохранённое одним JSON (байты) — под замком, пока никто не пишет."""
        with self._lock:
            payload = dict(self._load())
            payload.update(extra or {})
            return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def update(self, body, sources, keys, categories):
        """body: {"version", "feats": {ключ: {src: {"url", "f"}}}, "learn": {"key", "f"},
        "banner": {"bucket", "category"}}.
        sources: {ключ: {src: адрес}} — картинки известных предметов; keys — все известные
        предметы (выученная картинка бывает и у предмета без картинки на сайте);
        categories — известные категории. Возвращает, сколько записей сохранено."""
        if body.get("version") != FEAT_VERSION:
            raise ValueError("другая версия признаков картинок — обновите страницу")
        with self._lock:
            data = self._load()
            changed = 0
            feats = body.get("feats")
            for key, entries in (feats.items() if isinstance(feats, dict) else ()):
                if key not in sources or not isinstance(entries, dict):
                    continue
                for src, entry in entries.items():
                    url = sources[key].get(src) if src in SOURCES else None
                    if not url or not isinstance(entry, dict) or entry.get("url") != url:
                        continue  # картинка предмета сменилась — старые признаки не нужны
                    if _feature_ok(entry.get("f")):
                        data["feats"].setdefault(key, {})[src] = {"url": url, "f": entry["f"]}
                        changed += 1
            learn = body.get("learn")
            if isinstance(learn, dict) and learn.get("key") in keys and _feature_ok(learn.get("f")):
                learned = data["learned"].setdefault(learn["key"], [])
                if learn["f"] not in learned:
                    learned.append(learn["f"])
                    del learned[:-MAX_LEARNED]
                    changed += 1
            banner = body.get("banner")
            if (isinstance(banner, dict) and isinstance(banner.get("bucket"), str)
                    and _BUCKET_RE.fullmatch(banner["bucket"]) and banner.get("category") in categories):
                bucket = data["banners"].setdefault(banner["bucket"], {})
                bucket[banner["category"]] = min(10 ** 6, bucket.get(banner["category"], 0) + 1)
                changed += 1
            if changed:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(self.path, json.dumps(data, ensure_ascii=False).encode("utf-8"))
            return changed
