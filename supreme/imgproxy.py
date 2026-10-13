"""Картинки предметов через свой сервер — для сравнения плиток со скриншотов по картинке.

Браузер может читать пиксели картинки (canvas) только со своего адреса: картинка
с supremevalues.com или cdn.starpets.gg «портит» canvas. Поэтому картинки предметов
отдаются с 127.0.0.1: сервер скачивает их сам, проверяет и хранит в папке с ценами
(data/images), чтобы скачивать один раз. Признаки картинок (их считает браузер,
web/picmatch.js) хранятся в data/picfeat.json — тоже считаются один раз.

Это не открытый прокси: адрес берётся только из данных известного предмета (по его
ключу), только https и только с supremevalues.com и starpets.gg.
"""

import contextlib
import hashlib
import http.client
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
PAGE_BYTES = 64 * 1024             # страница вместо картинки читается до стольких байт (ищется защита сайта)
TIMEOUT = 10
FAIL_RETRY = 6 * 3600              # адрес, который ответил «нет такой картинки» или отказал, не запрашивается 6 часов
BUSY_RETRY = 10 * 60               # сайт перегружен (5xx) или картинка пришла не целиком — 10 минут
BLOCK_PAUSE = 30 * 60              # сайт не пускает программу (429, страница защиты) — весь сайт на 30 минут
DENIED_STATUSES = (401, 403, 451)  # отказ в одной картинке: CDN так отвечает и на «нет такой картинки»
DENIED_AFTER = 3                   # столько разных картинок подряд сайт не отдал — он не пускает программу
OFFLINE_AFTER = 4                  # столько обрывов связи с сайтом подряд — связи с ним нет:
OFFLINE_PAUSE = 60                 # минуту его картинки не запрашиваются (ответ сразу, без ожидания)
PARALLEL = 4                       # одновременных загрузок со всех сайтов
# Бережно к сайтам: (загрузок одновременно, секунд между началами загрузок). supremevalues.com
# под защитой от ботов и отдаёт ещё и цены: поток запросов закрыл бы программе и их. Картинки
# StarPets раздаёт CDN.
PACE = {"supremevalues.com": (2, 0.4), "starpets.gg": (3, 0.1)}
PACE_DEFAULT = (2, 0.4)
SLOW_MAX = 5.0                     # сайт занят или рвёт связь — промежуток удваивается, но не больше 5 с
SESSION_CAP = {"supremevalues.com": 300}  # загрузок с сайта за запуск программы; остальные — в следующий раз
# Страница защиты от ботов (Imperva у supremevalues.com, Cloudflare) вместо картинки.
_CHALLENGE = (b"_incapsula_resource", b"request unsuccessful", b"incapsula incident", b"cf-chl",
              b"challenge-platform", b"just a moment...", b"attention required! | cloudflare", b"captcha")
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
    """kind: "final" — картинки нет (запомнить надолго), "denied" — сайт не отдал эту картинку
    (запомнить надолго; несколько разных подряд — сайт не пускает программу), "busy" — сайт занят
    (запомнить ненадолго), "network" — нет связи с сайтом (не запоминать для адреса), "blocked" —
    сайт не пускает программу (пауза для всего сайта: тысяча запросов подряд к защищённому сайту
    только продлит блокировку), "capped" — с сайта на этот запуск скачано достаточно."""

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


def complete(data, ext):
    """Картинка пришла целиком? Обрыв связи посреди загрузки даёт начало файла с верными
    первыми байтами: такой файл не открывается (или открывается наполовину серым), и в кэше
    он остался бы навсегда. Лишние байты после конца картинки — не беда."""
    if ext == ".png":  # блок IEND (длина 0, тип, контрольная сумма)
        end = data.rfind(b"\x00\x00\x00\x00IEND")
        return end >= 8 and end + 12 <= len(data)
    if ext == ".webp":
        return len(data) >= 12 and int.from_bytes(data[4:8], "little") + 8 <= len(data)
    if ext == ".jpg":  # конец картинки (FF D9) — после последнего блока сжатых данных (FF DA)
        return data.rfind(b"\xff\xd9") > max(1, data.rfind(b"\xff\xda"))
    if ext == ".gif":
        return b";" in data[-32:]
    if ext == ".avif":  # коробки ISO BMFF: каждая целиком, среди них — данные картинки (mdat)
        pos, seen = 0, set()
        while pos + 8 <= len(data):
            size = int.from_bytes(data[pos:pos + 4], "big")
            if size == 1:
                size = int.from_bytes(data[pos + 8:pos + 16], "big")
            elif size == 0:
                return True  # последняя коробка — до конца файла
            if size < 8:
                break  # дальше не коробки — лишние байты после картинки
            if pos + size > len(data):
                return False
            seen.add(data[pos + 4:pos + 8])
            pos += size
        return b"mdat" in seen
    return False


def site_of(url):
    """Доверенный сайт адреса (supremevalues.com / starpets.gg): связь, пауза и бережность —
    общие для его поддоменов."""
    host = (urllib.parse.urlsplit(str(url)).hostname or "").lower().rstrip(".")
    return next((site for site in ALLOWED_SITES if host == site or host.endswith("." + site)), host)


def quote_url(url):
    """Пробелы и не-ASCII в адресе — в %-коды: у StarPets бывают адреса вроде
    «.../110x110/Luger chroma.webp», а с пробелом urllib запрос не отправит."""
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parts.path, safe="/%:@!$&'()*+,;=~")
    query = urllib.parse.quote(parts.query, safe="/%:@!$&'()*+,;=~?")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, ""))


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


def _challenge(page):
    """Страница защиты от ботов вместо картинки?"""
    page = (page or b"")[:PAGE_BYTES].lower()
    return any(marker in page for marker in _CHALLENGE)


def _error_page(error):
    try:
        return error.read(PAGE_BYTES) or b""
    except Exception:  # тело ответа с ошибкой не прочиталось — без него
        return b""


def _download(url, opener=None):
    """Скачать картинку: (байты, (content-type, расширение)). ImageError — если это не картинка."""
    opener = opener or urllib.request.build_opener(_CheckedRedirects)
    try:
        request = urllib.request.Request(quote_url(url), headers=HEADERS)
        with opener.open(request, timeout=TIMEOUT) as response:
            declared = (response.headers.get("Content-Length") or "").strip()
            declared = int(declared) if declared.isdigit() else None
            if declared is not None and declared > MAX_BYTES:
                raise ImageError(502, "картинка слишком большая")
            ctype = (response.headers.get_content_type() or "").lower()
            if ctype in ("text/html", "application/xhtml+xml"):
                # Страница вместо картинки: защита сайта (не пускает программу) — или так сайт
                # отвечает на этот адрес (нет такой картинки): тогда виноват только адрес.
                if _challenge(response.read(PAGE_BYTES)):
                    raise ImageError(429, "сайт картинок не пускает программу (страница защиты)", "blocked")
                raise ImageError(502, "сайт вернул страницу вместо картинки", "denied")
            if not (ctype.startswith("image/") or ctype in ("application/octet-stream", "binary/octet-stream")):
                raise ImageError(502, f"сайт вернул не картинку ({ctype})")
            body = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        if error.code == 429 or _challenge(_error_page(error)):
            raise ImageError(429, f"сайт картинок не пускает программу (HTTP {error.code})", "blocked") from None
        if error.code in DENIED_STATUSES:
            raise ImageError(502, f"сайт картинок не отдал картинку (HTTP {error.code})", "denied") from None
        busy = error.code >= 500
        status = 404 if error.code in (404, 410) else 502
        raise ImageError(status, f"сайт картинок ответил HTTP {error.code}", "busy" if busy else "final") from None
    except http.client.InvalidURL as error:
        raise ImageError(502, f"неверный адрес картинки ({error})") from None
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as error:
        # HTTPException: оборванный ответ (IncompleteRead), испорченные заголовки — тоже обрыв связи
        raise ImageError(503, f"нет связи с сайтом картинок ({error})", "network") from None
    if len(body) > MAX_BYTES:
        raise ImageError(502, "картинка слишком большая")
    if declared is not None and len(body) != declared:  # связь оборвалась: read() не жалуется, просто отдаёт меньше
        raise ImageError(503, "картинка пришла не целиком (обрыв связи)", "network")
    kind = sniff(body)
    if kind is None:
        raise ImageError(502, "сайт вернул не картинку")
    if not complete(body, kind[1]):
        # Длина совпала с объявленной — такая картинка на самом сайте (испорчена): надолго.
        # Длины не было — скорее связь оборвалась: попробовать позже.
        if declared is not None:
            raise ImageError(502, "картинка на сайте испорчена (не целиком)")
        raise ImageError(502, "картинка пришла не целиком", "busy")
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
    """Картинки на диске: <папка>/<sha1 адреса>.<расширение>; неудачи — в failed.json.
    С каждым сайтом — отдельно: бережно (PACE, SESSION_CAP), своя связь и своя пауза, если он
    не пускает программу: один недоступный сайт не мешает брать картинки с другого."""

    def __init__(self, folder, download=_download, clock=time.time, sleep=time.sleep):
        self.folder = Path(folder)
        self._download = download
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._url_locks = {}
        self._slots = threading.BoundedSemaphore(PARALLEL)
        self._failed = None
        self._size = None
        self._site_slots = {}      # сайт -> сколько загрузок с него одновременно
        self._next_start = {}      # сайт -> не раньше какого времени начать следующую загрузку
        self._slow = {}            # сайт -> промежуток между загрузками, увеличенный после ошибок
        self._fetched = {}         # сайт -> загрузок за этот запуск программы
        self._network_errors = {}  # сайт -> обрывов связи подряд
        self._offline_until = {}   # сайт -> до какого времени связи с ним нет
        self._denied = {}          # сайт -> сколько разных картинок подряд он не отдал
        self._paused = {}          # сайт -> до какого времени не запрашивать (не пускает программу)

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

    def _capped(self, site):
        return self._fetched.get(site, 0) >= SESSION_CAP.get(site, float("inf"))

    def get(self, url):
        """(байты, content-type). ImageError — если картинку не получить."""
        if not host_allowed(url):
            raise ImageError(403, "картинки берутся только с supremevalues.com и starpets.gg")
        site = site_of(url)
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
                if kind and complete(data, kind[1]):
                    return data, kind[0]
            with self._lock:
                failed = self._failures().get(url)
                now = self._clock()
                offline = now < self._offline_until.get(site, 0.0)
                paused = now < self._paused.get(site, 0.0)
                capped = self._capped(site)
            if failed:
                at, kind = failed
                if self._clock() - at < (BUSY_RETRY if kind == "busy" else FAIL_RETRY):
                    raise ImageError(404 if kind == "final" else 502, "картинка недавно не загрузилась", kind)
            if offline:
                raise ImageError(503, "нет связи с сайтом картинок", "network")
            if paused:
                raise ImageError(429, "сайт картинок не пускает программу — попробую позже", "blocked")
            if capped:
                raise ImageError(429, "картинок с этого сайта на этот запуск достаточно — остальные в следующий раз", "capped")
            with self._polite(site):
                try:
                    data, (ctype, ext) = self._download(url)
                except ImageError as error:
                    self._after_error(url, site, error)
                    raise
            with self._lock:  # сайт ответил: связь есть, и он пускает программу
                self._network_errors[site] = 0
                self._denied[site] = 0
                self._slow.pop(site, None)
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                _atomic_write(self.folder / (self._name(url) + ext), data)
                self._added(len(data))
            except OSError:
                pass  # не сохранилась — отдадим и так
            return data, ctype

    @contextlib.contextmanager
    def _polite(self, site):
        """Загрузка с сайта: не больше PACE[сайт] одновременно, начала — не чаще промежутка
        (после ошибок он больше) и не больше SESSION_CAP[сайт] загрузок за запуск."""
        parallel, gap = PACE.get(site, PACE_DEFAULT)
        with self._lock:
            slots = self._site_slots.setdefault(site, threading.BoundedSemaphore(parallel))
        with slots:
            with self._lock:
                if self._capped(site):
                    raise ImageError(429, "картинок с этого сайта на этот запуск достаточно — остальные в следующий раз",
                                     "capped")
                self._fetched[site] = self._fetched.get(site, 0) + 1
                now = self._clock()
                start = max(now, self._next_start.get(site, 0.0))
                self._next_start[site] = start + max(gap, self._slow.get(site, 0.0))
            if start > now:
                self._sleep(start - now)
            with self._slots:
                yield

    def _after_error(self, url, site, error):
        with self._lock:
            if error.kind in ("busy", "network"):  # сайт занят или рвёт связь — к нему реже
                gap = PACE.get(site, PACE_DEFAULT)[1]
                self._slow[site] = min(SLOW_MAX, max(gap, self._slow.get(site, 0.0)) * 2)
            if error.kind == "network":
                errors = self._network_errors.get(site, 0) + 1
                if errors >= OFFLINE_AFTER:
                    self._offline_until[site] = self._clock() + OFFLINE_PAUSE
                    errors = 0
                self._network_errors[site] = errors
                return  # связь с сайтом пропала — сам адрес не виноват
            self._network_errors[site] = 0  # сайт ответил — связь с ним есть
            if error.kind == "blocked":
                self._paused[site] = self._clock() + BLOCK_PAUSE
                self._denied[site] = 0
                return  # не пускает весь сайт — сам адрес не виноват
            if error.kind == "denied":  # отказ в одной картинке; в нескольких разных подряд — не пускает
                denied = self._denied.get(site, 0) + 1
                if denied >= DENIED_AFTER:
                    self._paused[site] = self._clock() + BLOCK_PAUSE
                    denied = 0
                self._denied[site] = denied
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
MAX_LEARN_POST = 1000       # записей "learn" и "banner" в одном запросе (браузер шлёт их пачкой)
SOURCES = ("site", "market")
_BUCKET_RE = re.compile(r"grey|rainbow|\d{1,3}")
_FEAT_RE = re.compile(r"[A-Za-z0-9+/]+={0,2}")


def _feature_ok(value):
    return isinstance(value, str) and 0 < len(value) <= MAX_FEAT_CHARS and bool(_FEAT_RE.fullmatch(value))


def _entries(value):
    """Запись или список записей (словарей) из запроса; остальное отбрасывается."""
    listed = value[:MAX_LEARN_POST] if isinstance(value, list) else [value]
    return [entry for entry in listed if isinstance(entry, dict)]


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
        """body: {"version", "feats": {ключ: {src: {"url", "f"}}}, "learn": {"key", "f"} или их список,
        "banner": {"bucket", "category"} или их список}. Файл переписывается целиком — один раз на запрос.
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
            for learn in _entries(body.get("learn")):
                if not (isinstance(learn.get("key"), str) and learn["key"] in keys and _feature_ok(learn.get("f"))):
                    continue
                learned = data["learned"].get(learn["key"])
                if not isinstance(learned, list):
                    learned = data["learned"][learn["key"]] = []
                if learn["f"] not in learned:
                    learned.append(learn["f"])
                    del learned[:-MAX_LEARNED]
                    changed += 1
            for banner in _entries(body.get("banner")):
                bucket, category = banner.get("bucket"), banner.get("category")
                if not (isinstance(bucket, str) and _BUCKET_RE.fullmatch(bucket)
                        and isinstance(category, str) and category in categories):
                    continue
                counts = data["banners"].get(bucket)
                if not isinstance(counts, dict):
                    counts = data["banners"][bucket] = {}
                counts[category] = min(10 ** 6, (counts.get(category) if isinstance(counts.get(category), int) else 0) + 1)
                changed += 1
            if changed:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(self.path, json.dumps(data, ensure_ascii=False).encode("utf-8"))
            return changed
