"""Локальный веб-сервер программы: http://127.0.0.1:<порт>/"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import liquidity, parser, paths, store

WEB_DIR = paths.resource_dir() / "web"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
MAX_BODY = 5 * 1024 * 1024


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


def build_payload(data):
    items = []
    for item in data.get("items", []):
        item = dict(item)
        item["liquidity"] = liquidity.assess(item)
        items.append(item)
    counts = {}
    for item in items:
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    return {
        "site_last_updated": data.get("site_last_updated"),
        "fetched_at": data.get("fetched_at"),
        "source": data.get("source"),
        "errors": data.get("errors", []),
        "categories": [
            {"slug": slug, "title": title, "count": counts.get(slug, 0), "weapon": slug in parser.WEAPON_CATEGORIES}
            for slug, title in parser.CATEGORIES
        ],
        "items": items,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "MM2Values"
    job = None  # UpdateJob, задаётся в make_server
    data_path = None

    def log_message(self, format, *args):
        pass  # не засорять консоль строками каждого запроса

    # --- проверки --------------------------------------------------------

    def _allowed_hosts(self):
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _host_ok(self):
        return self.headers.get("Host", "") in self._allowed_hosts()

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://{host}" for host in self._allowed_hosts()}

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
                data = store.load(self.data_path)
            except (OSError, ValueError) as error:
                return self._error(500, f"Файл с ценами повреждён: {error}")
            return self._json(200, build_payload(data))
        if path == "/api/status":
            return self._json(200, self.job.status())
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
        if length < 0 or length > MAX_BODY:
            return self._error(413, "Слишком большой запрос")
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            return self._error(400, "Неверный JSON")
        if not isinstance(body, dict):
            return self._error(400, "Неверный JSON")

        path = self.path.split("?", 1)[0]
        if path == "/api/update":
            started = self.job.start()
            return self._json(202, {"started": started, **self.job.status()})
        if path == "/api/import":
            if self.job.running:
                return self._error(409, "Сейчас идёт обновление с сайта, попробуйте после него")
            text = body.get("text")
            category = body.get("category")
            if not isinstance(text, str) or not isinstance(category, str):
                return self._error(400, "Нужны поля text и category")
            try:
                items = store.import_text(text, category, self.data_path)
            except ValueError as error:
                return self._error(400, str(error))
            return self._json(200, {"imported": len(items), "category": category})
        return self._error(404, "Не найдено")


def make_server(host="127.0.0.1", port=8765, data_path=None, update=None, tries=20):
    """Создать сервер; если порт занят — взять следующий свободный."""
    job = UpdateJob(update or (lambda log: store.update_from_site(log=log, path=data_path)))
    handler = type("BoundHandler", (Handler,), {"job": job, "data_path": data_path})
    last_error = None
    for offset in range(tries):
        try:
            server = ThreadingHTTPServer((host, port + offset if port else 0), handler)
        except OSError as error:
            last_error = error
            if not port:
                break
            continue
        server.daemon_threads = True
        server.job = job
        return server
    raise OSError(f"Не удалось занять порт для программы: {last_error}")
