"""Локальный веб-сервер программы: http://127.0.0.1:<порт>/"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import base64
import binascii

from . import autosync, calc, liquidity, markets, ocr, parser, paths, store

WEB_DIR = paths.resource_dir() / "web"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
MAX_BODY = 5 * 1024 * 1024
MAX_CALC_BODY = 64 * 1024 * 1024  # несколько скриншотов (интерфейс уменьшает большие)
DRAIN_LIMIT = 256 * 1024 * 1024  # слишком большой запрос дочитывается, чтобы браузер увидел ответ
MAX_IMAGES = 8


class _ExclusiveServer(ThreadingHTTPServer):
    # На Windows SO_REUSEADDR позволяет занять уже занятый порт — вторая копия
    # программы «запустилась бы» на том же порту. Здесь занятый порт — ошибка.
    allow_reuse_address = False


class UpdateJob:
    """Обновление цен в фоне, чтобы окно программы не зависало."""

    def __init__(self, update=store.update_from_site):
        self._update = update
        self._lock = threading.Lock()
        self.running = False
        self.log = []
        self.error = None
        self.finished_at = None

    def start(self):
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.log = []
            self.error = None
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _add_log(self, line):
        with self._lock:
            self.log.append(str(line))
            del self.log[:-300]

    def _run(self):
        error = None
        try:
            self._update(log=self._add_log)
        except Exception as exc:  # показать любую ошибку в окне программы
            error = str(exc) or exc.__class__.__name__
        with self._lock:
            self.error = error
            self.running = False
            self.finished_at = store.now_iso()

    def status(self):
        with self._lock:
            return {
                "running": self.running,
                "log": list(self.log),
                "error": self.error,
                "finished_at": self.finished_at,
            }


def build_payload(data, load_error=None, monitor=None, rate=None):
    items = []
    for item in data.get("items", []):
        if parser.is_placeholder(item):
            continue  # надписи со страницы без спроса и редкости
        item = dict(item)
        item["liquidity"] = liquidity.assess(item)
        items.append(item)
    market_error = None
    if monitor is not None:
        try:
            monitor.annotate(items)
        except Exception as error:  # данные площадок не должны прятать цены
            market_error = str(error) or type(error).__name__
    counts = {}
    for item in items:
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    category_info = data.get("categories") or {}
    errors = {error.get("category"): error.get("message") for error in data.get("errors", []) if isinstance(error, dict)}
    return {
        "site_last_updated": data.get("site_last_updated"),
        "fetched_at": data.get("fetched_at"),
        "source": data.get("source"),
        "load_error": load_error,
        "errors": [error for error in data.get("errors", []) if isinstance(error, dict)],
        "categories": [
            {
                "slug": slug,
                "title": title,
                "count": counts.get(slug, 0),
                "weapon": slug in parser.WEAPON_CATEGORIES,
                "updated_at": (category_info.get(slug) or {}).get("updated_at"),
                "source": (category_info.get(slug) or {}).get("source"),
                "error": errors.get(slug),
            }
            for slug, title in parser.CATEGORIES
        ],
        "liquidity": {
            "liquid_from": liquidity.LIQUID_FROM,
            "medium_from": liquidity.MEDIUM_FROM,
            "stability": {name: bonus for name, (bonus, _) in liquidity.STABILITY.items()},
        },
        "markets": monitor.status_list() if monitor is not None else [],
        "market_error": market_error,
        "rate": rate.status() if rate is not None else None,
        "fees": {m["id"]: m.get("fee", 0) for m in (monitor.configs() if monitor is not None else [])},
        "items": items,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "MM2Values"
    job = None  # UpdateJob, задаётся в make_server
    sync = None  # AutoSync или None
    monitor = None  # MarketMonitor или None
    rate = None  # RateWatcher или None
    data_path = None

    def log_message(self, format, *args):
        pass  # не засорять консоль строками каждого запроса

    # --- проверки --------------------------------------------------------

    def _local_address(self, value):
        """"127.0.0.1:8765" / "localhost" (порт 80) -> адрес этой программы?"""
        host, _, port = value.strip().lower().rpartition(":") if ":" in value else (value.strip().lower(), "", "")
        if not host:
            host, port = port, ""
        server_port = self.server.server_address[1]
        if port and port != str(server_port):
            return False
        if not port and server_port != 80:
            return False
        return host in ("127.0.0.1", "localhost")

    def _host_ok(self):
        return self._local_address(self.headers.get("Host", ""))

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return origin.lower().startswith("http://") and self._local_address(origin[len("http://"):].rstrip("/"))

    # --- ответы ----------------------------------------------------------

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status, message):
        self._json(status, {"error": message})

    def _drain(self, length):
        left = length
        while left > 0:
            chunk = self.rfile.read(min(left, 1024 * 1024))
            if not chunk:
                break
            left -= len(chunk)

    def _calc(self, body):
        """Калькулятор продажи: текст и/или скриншоты -> предметы с ценами площадок."""
        text = body.get("text") if isinstance(body.get("text"), str) else ""
        images = body.get("images") if isinstance(body.get("images"), list) else []
        if len(images) > MAX_IMAGES:
            return self._error(400, f"Не больше {MAX_IMAGES} скриншотов за раз")
        ocr_lines, ocr_errors = [], []
        for index, image in enumerate(images, 1):
            if not isinstance(image, str):
                continue
            try:
                raw = base64.b64decode(image.split(",", 1)[-1], validate=False)
            except (binascii.Error, ValueError):
                ocr_errors.append(f"скриншот {index}: не картинка")
                continue
            try:
                ocr_lines.extend(ocr.recognize(raw))
            except ocr.OcrError as error:
                ocr_errors.append(f"скриншот {index}: {error}")
            except Exception as error:  # одна картинка не должна ломать весь расчёт
                ocr_errors.append(f"скриншот {index}: не удалось распознать ({error})")
        if not text.strip() and not ocr_lines:
            message = "; ".join(ocr_errors) or "Вставьте список предметов или добавьте скриншот"
            return self._error(400, message)
        try:
            data = store.load(self.data_path)
        except (OSError, ValueError) as error:
            return self._error(500, f"Не удалось прочитать цены: {error}")
        items = [dict(i, liquidity=liquidity.assess(i)) for i in data["items"] if not parser.is_placeholder(i)]
        if self.monitor is not None:
            try:
                self.monitor.annotate(items)
            except Exception:  # без цен площадок, но с найденными предметами
                pass
        found, unmatched = calc.match_text(text + "\n" + "\n".join(ocr_lines), items)
        result = []
        for entry in found:
            item = entry["item"]
            market = item.get("market") or {}
            result.append({
                "name": item["name"],
                "category": item["category"],
                "image": item.get("image") or item.get("image_market"),
                "value": item.get("value"),
                "qty": entry["qty"],
                "score": round(entry["score"], 2),
                "seen": entry["seen"],
                "prices": {
                    market_id: {"price": info.get("price"), "currency": info.get("currency"),
                                "fee": info.get("fee", 0), "stock": info.get("stock")}
                    for market_id, info in market.items()
                },
            })
        return self._json(200, {
            "items": result,
            "unmatched": unmatched[:50],
            "ocr_lines": len(ocr_lines),
            "ocr_errors": ocr_errors,
            "rate": self.rate.status() if self.rate else None,
            "fees": {m["id"]: m.get("fee", 0) for m in (self.monitor.configs() if self.monitor else [])},
        })

    def _status(self):
        status = self.job.status()
        status["now"] = store.now_iso()
        status["sync"] = self.sync.status() if self.sync else None
        status["markets"] = self.monitor.status_list() if self.monitor else []
        status["market_version"] = self.monitor.version if self.monitor else 0
        status["rate"] = self.rate.status() if self.rate else None
        try:
            status["data_version"] = os.stat(self.data_path or store.DATA_FILE).st_mtime_ns
        except OSError:
            status["data_version"] = 0
        return status

    # --- маршруты --------------------------------------------------------

    def do_GET(self):
        if not self._host_ok():
            return self._error(403, "Неверный адрес")
        path = self.path.split("?", 1)[0]
        if path in STATIC_FILES:
            name, content_type = STATIC_FILES[path]
            return self._send(200, (WEB_DIR / name).read_bytes(), content_type)
        if path == "/api/data":
            try:
                payload = build_payload(store.load(self.data_path), monitor=self.monitor, rate=self.rate)
            except PermissionError as error:
                payload = build_payload(store.empty_data(), f"Файл с ценами сейчас занят ({error}). Обновите страницу.")
            except Exception as error:
                # Показать пустую программу с ошибкой: обновление или импорт заменят файл.
                payload = build_payload(
                    store.empty_data(),
                    f"Файл с ценами повреждён ({error}). Нажмите «Обновить цены» или загрузите цены через «Импорт».",
                )
            return self._json(200, payload)
        if path == "/api/status":
            return self._json(200, self._status())
        return self._error(404, "Не найдено")

    def do_POST(self):
        if not (self._host_ok() and self._origin_ok()):
            return self._error(403, "Запрос с чужого сайта запрещён")
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._error(415, "Нужен JSON")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._error(400, "Неверная длина запроса")
        limit = MAX_CALC_BODY if self.path.split("?", 1)[0] == "/api/calc" else MAX_BODY
        if length < 0 or length > limit:
            if 0 < length <= DRAIN_LIMIT:
                self._drain(length)  # иначе браузер видит обрыв связи вместо сообщения
            else:
                self.close_connection = True
            message = ("Скриншоты слишком большие: добавьте меньше за раз" if limit == MAX_CALC_BODY
                       else "Слишком большой запрос")
            return self._error(413, message)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            return self._error(400, "Неверный JSON")
        if not isinstance(body, dict):
            return self._error(400, "Неверный JSON")

        path = self.path.split("?", 1)[0]
        if path == "/api/update":
            started = self.job.start()
            return self._json(202, {"started": started, **self._status()})
        if path == "/api/calc":
            return self._calc(body)
        if path == "/api/markets/poll":
            if self.monitor is None:
                return self._error(409, "Площадки выключены")
            self.monitor.poll_now()
            return self._json(202, self._status())
        if path == "/api/check":
            if self.sync is None:
                return self._error(409, "Автообновление выключено")
            self.sync.check_now()
            return self._json(202, self._status())
        if path == "/api/import":
            text = body.get("text")
            category = body.get("category")
            if not isinstance(text, str) or not isinstance(category, str):
                return self._error(400, "Нужны поля text и category")
            try:
                items, last_updated = store.import_text(text, category, self.data_path)
            except ValueError as error:
                return self._error(400, str(error))
            except OSError as error:
                return self._error(500, f"Не удалось сохранить цены: {error}")
            except Exception as error:  # ответить понятной ошибкой, а не обрывом соединения
                return self._error(500, f"Ошибка импорта: {error}")
            return self._json(200, {"imported": len(items), "category": category, "site_last_updated": last_updated})
        return self._error(404, "Не найдено")


def make_server(host="127.0.0.1", port=8765, data_path=None, update=None, tries=20, auto_sync=False,
                check_site=None, monitor=None, rate=None):
    """Создать сервер; если порт занят — взять следующий свободный.

    auto_sync=True — следить за сайтом и обновлять цены автоматически
    (поток запускается методом server.sync.start()).
    """
    job = UpdateJob(update or (lambda log: store.update_from_site(log=log, path=data_path)))
    sync = None
    if auto_sync:
        sync = autosync.AutoSync(job, data_path, **({"check_site": check_site} if check_site else {}))
    handler = type(
        "BoundHandler", (Handler,),
        {"job": job, "sync": sync, "monitor": monitor, "rate": rate, "data_path": data_path},
    )
    server_class = _ExclusiveServer if os.name == "nt" else ThreadingHTTPServer
    last_error = None
    for offset in range(tries):
        try:
            server = server_class((host, port + offset if port else 0), handler)
        except OSError as error:
            last_error = error
            if not port:
                break
            continue
        server.daemon_threads = True
        server.job = job
        server.sync = sync
        server.monitor = monitor
        server.rate = rate
        return server
    raise OSError(f"Не удалось занять порт для программы: {last_error}")
