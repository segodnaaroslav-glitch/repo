"""Скрытый Edge/Chrome по протоколу DevTools — стандартной библиотекой Python.

Нужен для площадок, которые рисуют список товаров скриптом или не пускают
обычные запросы: браузер открывает страницу как обычный посетитель, а программа
читает из неё готовые карточки и выполняет запросы изнутри страницы.

Связь с браузером:

* Linux и macOS — пара каналов (--remote-debugging-pipe). Других программ к ним
  не подключить, а браузер сам закрывается, когда программа завершилась.
* Windows — порт DevTools на 127.0.0.1. Пока идёт проход (обычно меньше пары
  минут), к этому порту без пароля может подключиться любая программа этого
  компьютера, в том числе другого пользователя на общем или RDP-компьютере, и
  управлять браузером от имени пользователя программы (открывать file:///,
  делать запросы с cookies площадки). Поэтому проход короткий, браузер
  закрывается сразу после чтения данных, профиль у него временный и пустой, а
  процесс входит в задание Windows (Job Object), которое закрывает браузер
  вместе с программой, даже если её окно просто закрыли.

Открытые браузеры записаны в _LIVE: close_all() и выход из программы (atexit)
закрывают их сразу. Профили, оставшиеся после аварийного завершения, удаляются
при следующем запуске браузера (старше часа).
"""

import atexit
import base64
import json
import os
import re
import selectors
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request

from . import fetcher

PROFILE_PREFIX = "mm2values-cdp-"
STALE_PROFILE_AGE = 60 * 60  # профиль старше часа — остался от аварийного выхода
ACCEPT_LANGUAGE = "ru-RU,ru,en"  # браузер сам допишет веса: "ru-RU,ru;q=0.9,en;q=0.8"
MAX_PAGE_CHARS = 500_000  # HTML одной страницы после чистки (заголовок, meta, JSON-данные, текст)
MAX_DATA_CHARS = 4_000_000  # ответ с данными (JSON)
MAX_TOTAL_CHARS = 30_000_000  # всего за один fetch_many — проход не тащит сотни мегабайт
# Страница перешла на другой адрес, пока выполнялся скрипт (проверка площадки, переадресация).
NAVIGATED_RE = re.compile(r"navigated|context was destroyed|Cannot find context|Promise was collected", re.I)
# Долгие запросы-подписки: они не заканчиваются, а данных о товарах в них нет.
STREAM_URL_RE = re.compile(r"socket\.io|transport=(?:polling|websocket)|event-?stream|/sse(?:[/?]|$)|"
                           r"long-?poll|/ws(?:[/?]|$)|^wss?:", re.I)

_LIVE = set()  # открытые браузеры
_LIVE_LOCK = threading.Lock()
_STATE = {"exiting": False, "swept": False, "job": None}
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 127.0.0.1 — мимо прокси системы


class BrowserError(Exception):
    pass


class ClosedError(BrowserError):
    """Браузер закрылся (или его закрыли): дальше с ним говорить нельзя."""


class NavigatedError(BrowserError):
    """Пока выполнялся скрипт, страница ушла на другой адрес."""


def _mask(data, key):
    if not data:
        return b""
    pad = (key * (len(data) // 4 + 1))[:len(data)]
    return (int.from_bytes(data, "big") ^ int.from_bytes(pad, "big")).to_bytes(len(data), "big")


class WebSocket:
    """Минимальный клиент WebSocket (RFC 6455) для соединения с браузером на 127.0.0.1."""

    CHUNK = 1 << 20

    def __init__(self, url, timeout=30):
        parts = urllib.parse.urlsplit(url)
        deadline = time.time() + timeout
        self.sock = socket.create_connection((parts.hostname, parts.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = parts.path + (("?" + parts.query) if parts.query else "")
        request = (f"GET {path} HTTP/1.1\r\nHost: {parts.hostname}:{parts.port}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.buffer = bytearray()
        self.parts = bytearray()  # начало сообщения, разбитого на несколько кадров
        try:
            self.sock.sendall(request.encode())
            while b"\r\n\r\n" not in self.buffer:
                if len(self.buffer) > 65536:
                    raise BrowserError("браузер не открыл соединение")
                self._fill(deadline)
            end = self.buffer.index(b"\r\n\r\n")
            head = bytes(self.buffer[:end])
            del self.buffer[:end + 4]
            if b" 101 " not in head.split(b"\r\n", 1)[0]:
                raise BrowserError("браузер не открыл соединение: " + head.decode("latin-1")[:200])
        except BaseException:
            self.close()
            raise

    def _fill(self, deadline):
        """Дочитать из сокета; срок — общий на всё сообщение, а не на один recv."""
        left = deadline - time.time()
        if left <= 0:
            raise TimeoutError("браузер не ответил вовремя")
        self.sock.settimeout(left)
        try:
            chunk = self.sock.recv(self.CHUNK)
        except socket.timeout as error:
            raise TimeoutError("браузер не ответил вовремя") from error
        except OSError as error:
            raise ClosedError(f"соединение с браузером закрыто: {error}") from error
        if not chunk:
            raise ClosedError("соединение с браузером закрыто")
        self.buffer += chunk

    def _take_frame(self):
        """Целый кадр из буфера: (последний?, код, данные) или None, если он пришёл не весь."""
        buffer = self.buffer
        if len(buffer) < 2:
            return None
        first, second = buffer[0], buffer[1]
        length, start = second & 0x7F, 2
        if length == 126:
            if len(buffer) < 4:
                return None
            length, start = struct.unpack_from(">H", buffer, 2)[0], 4
        elif length == 127:
            if len(buffer) < 10:
                return None
            length, start = struct.unpack_from(">Q", buffer, 2)[0], 10
        key = None
        if second & 0x80:
            if len(buffer) < start + 4:
                return None
            key, start = bytes(buffer[start:start + 4]), start + 4
        if len(buffer) < start + length:
            return None
        data = bytes(memoryview(buffer)[start:start + length])
        del buffer[:start + length]
        return bool(first & 0x80), first & 0x0F, _mask(data, key) if key else data

    def _frame(self, opcode, payload):
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", length)
        key = os.urandom(4)
        try:
            self.sock.sendall(bytes(header) + key + _mask(payload, key))
        except OSError as error:
            raise ClosedError(f"соединение с браузером закрыто: {error}") from error

    def send(self, text):
        self._frame(0x1, text.encode("utf-8"))

    def recv(self, timeout):
        deadline = time.time() + timeout
        while True:
            frame = self._take_frame()
            if frame is None:
                self._fill(deadline)
                continue
            last, opcode, data = frame
            if opcode == 0x8:
                raise ClosedError("браузер закрыл соединение")
            if opcode == 0x9:  # ping -> pong
                self._frame(0xA, data)
                continue
            if opcode == 0xA:
                continue
            self.parts += data
            if last:
                message, self.parts = bytes(self.parts), bytearray()
                return message.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Pipe:
    """Связь через пару каналов (--remote-debugging-pipe): сообщения JSON, разделитель — нулевой байт."""

    CHUNK = 1 << 20

    def __init__(self, read_fd, write_fd):
        self.read_fd, self.write_fd = read_fd, write_fd
        self.buffer = bytearray()
        self.scanned = 0  # до этого места разделителя в буфере точно нет
        self.selector = selectors.DefaultSelector()
        self.selector.register(read_fd, selectors.EVENT_READ)

    def send(self, text):
        view = memoryview(text.encode("utf-8") + b"\0")
        try:
            while view:
                view = view[os.write(self.write_fd, view):]
        except OSError as error:
            raise ClosedError(f"соединение с браузером закрыто: {error}") from error

    def recv(self, timeout):
        deadline = time.time() + timeout
        while True:
            end = self.buffer.find(b"\0", self.scanned)
            if end >= 0:
                message = bytes(memoryview(self.buffer)[:end])
                del self.buffer[:end + 1]
                self.scanned = 0
                return message.decode("utf-8", "replace")
            self.scanned = len(self.buffer)
            left = deadline - time.time()
            if left <= 0 or not self.selector.select(left):
                raise TimeoutError("браузер не ответил вовремя")
            try:
                chunk = os.read(self.read_fd, self.CHUNK)
            except OSError as error:
                raise ClosedError(f"соединение с браузером закрыто: {error}") from error
            if not chunk:
                raise ClosedError("соединение с браузером закрыто")
            self.buffer += chunk

    def close(self):
        fds, self.read_fd, self.write_fd = (self.read_fd, self.write_fd), None, None
        if fds[0] is None:
            return  # уже закрыт: второй os.close мог бы закрыть чужой файл с тем же номером
        try:
            self.selector.close()
        except (OSError, ValueError):
            pass
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass


class Browser:
    """Скрытый Edge/Chrome. Использовать через `with Browser(...) as browser:`.

    pipe: связь через каналы (по умолчанию — везде, кроме Windows) или через порт.
    """

    def __init__(self, executable=None, timeout=45, pipe=None):
        self.executable = executable or fetcher.find_system_browser()
        if not self.executable:
            raise BrowserError("на компьютере не найден Edge или Chrome")
        self.pipe = os.name != "nt" and os.path.exists("/bin/sh") if pipe is None else bool(pipe)
        self.process = None
        self.conn = None
        self.session = None
        self.next_id = 0
        self.events = []
        self.notes = []  # что пошло не так, но не помешало (для отчёта)
        self._lock = threading.Lock()
        sweep_stale_profiles()
        self.profile = tempfile.mkdtemp(prefix=PROFILE_PREFIX)
        try:
            self._launch()
            self._connect(timeout)
            self._attach(timeout)
            self._disguise()
        except Exception:
            self.close()
            raise

    # --- запуск ---

    def _command(self):
        command = [
            self.executable, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--disable-extensions", "--mute-audio", "--window-size=1366,900", "--lang=ru-RU",
            "--disable-blink-features=AutomationControlled", "--disable-background-networking",
            "--disable-component-update", "--disable-sync", "--disable-default-apps",
            f"--user-data-dir={self.profile}",
        ]
        if self.pipe:
            command.append("--remote-debugging-pipe")
        else:
            command += ["--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0"]
        if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
            command.insert(1, "--no-sandbox")
        return command + ["about:blank"]

    def _launch(self):
        command = self._command()
        with _LIVE_LOCK:
            if _STATE["exiting"]:
                raise BrowserError("программа закрывается")
            if self.pipe:
                browser_reads, we_write = os.pipe()
                we_read, browser_writes = os.pipe()
                try:
                    # Браузер ждёт каналы под номерами 3 и 4: оболочка переставляет их туда и запускает его.
                    self.process = subprocess.Popen(
                        ["/bin/sh", "-c", 'exec "$0" "$@" 3<&0 4>&1 0</dev/null 1>/dev/null'] + command,
                        stdin=browser_reads, stdout=browser_writes, stderr=subprocess.DEVNULL)
                except Exception:
                    os.close(we_read)
                    os.close(we_write)
                    raise
                finally:
                    os.close(browser_reads)
                    os.close(browser_writes)
                self.conn = Pipe(we_read, we_write)
            else:
                self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            _LIVE.add(self)
        _windows_job_assign(self.process)

    def _connect(self, timeout):
        if self.pipe:
            return
        port_file = os.path.join(self.profile, "DevToolsActivePort")
        deadline = time.time() + timeout
        lines, first_seen = [], None
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise BrowserError("браузер не запустился")
            try:
                with open(port_file, encoding="utf-8") as file:
                    lines = file.read().split()
            except OSError:
                lines = []
            first_seen = first_seen or (time.time() if lines else None)
            if len(lines) > 1 or (first_seen and time.time() - first_seen > 1):
                break  # вторая строка — адрес браузера; файл мог быть ещё дописан не весь
            time.sleep(0.1)
        if not lines:
            raise BrowserError("браузер не запустился вовремя")
        port = lines[0]
        if len(lines) > 1 and lines[1].startswith("/devtools/browser/"):
            url = f"ws://127.0.0.1:{port}{lines[1]}"
        else:
            url = None
            while time.time() < deadline and not url:
                try:
                    url = local_json(f"http://127.0.0.1:{port}/json/version").get("webSocketDebuggerUrl")
                except (OSError, ValueError, AttributeError):
                    time.sleep(0.3)
            if not url:
                raise BrowserError("браузер не открыл порт DevTools")
        self.conn = WebSocket(url, timeout=max(1, deadline - time.time()))

    def _attach(self, timeout):
        """Подключиться к вкладке браузера (одно соединение — и для браузера, и для вкладки)."""
        page = None
        for _ in range(15):
            try:
                targets = self.call("Target.getTargets", timeout=timeout, browser=True).get("targetInfos") or []
            except ClosedError as error:
                raise BrowserError("браузер не запустился") from error
            page = next((t for t in targets if t.get("type") == "page"), None)
            if page:
                break
            time.sleep(0.2)
        else:
            page = self.call("Target.createTarget", {"url": "about:blank"}, browser=True)
        self.session = self.call("Target.attachToTarget", {"targetId": page["targetId"], "flatten": True},
                                 browser=True)["sessionId"]
        self.call("Page.enable")
        self.call("Page.setLifecycleEventsEnabled", {"enabled": True})  # события с номером загрузки (loaderId)

    def _disguise(self):
        """Не выдавать себя: настоящая версия браузера без «HeadlessChrome» и те же
        подсказки клиента (Sec-CH-UA), что у обычного окна. navigator.webdriver
        выключен флагом --disable-blink-features=AutomationControlled."""
        user_agent = self.call("Browser.getVersion", browser=True).get("userAgent") or ""
        params = {"userAgent": user_agent.replace("HeadlessChrome", "Chrome").replace("Headless", ""),
                  "acceptLanguage": ACCEPT_LANGUAGE}
        try:
            metadata = self._native_metadata()
        except Exception as error:  # без них браузер всё равно работает
            metadata = None
            self.notes.append(f"подсказки клиента не прочитаны: {error}")
        if metadata:
            params["userAgentMetadata"] = metadata
        self.call("Emulation.setUserAgentOverride", params)

    def _native_metadata(self):
        """Подсказки клиента браузера (марки, версии, система) — со служебной страницы
        chrome://version: about:blank не «безопасная» страница, и navigator.userAgentData там нет."""
        result = self.call("Page.navigate", {"url": "chrome://version/"}, timeout=15)
        if result.get("errorText"):
            raise BrowserError(result["errorText"])
        deadline = time.time() + 10
        data = None
        while data is None and time.time() < deadline:
            try:
                data = self.evaluate(JS_UA_DATA, timeout=5)
            except NavigatedError:
                pass
            if data is None:
                time.sleep(0.1)
        if not isinstance(data, dict) or not data.get("brands"):
            return None

        def brands(values):
            return [{"brand": str(b.get("brand", "")).replace("HeadlessChrome", "Google Chrome").replace("Headless", ""),
                     "version": str(b.get("version", ""))} for b in values or [] if isinstance(b, dict)]

        return {"brands": brands(data.get("brands")), "fullVersionList": brands(data.get("fullVersionList")),
                "platform": str(data.get("platform") or ""), "platformVersion": str(data.get("platformVersion") or ""),
                "architecture": str(data.get("architecture") or ""), "model": str(data.get("model") or ""),
                "mobile": bool(data.get("mobile")), "bitness": str(data.get("bitness") or ""),
                "wow64": bool(data.get("wow64"))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- протокол ---

    def _next(self, deadline, what):
        left = deadline - time.time()
        if left <= 0:
            raise TimeoutError(what)
        if self.conn is None:
            raise ClosedError("браузер закрыт")
        try:
            message = json.loads(self.conn.recv(left))
        except TimeoutError as error:
            raise TimeoutError(what) from error
        except ValueError:
            return {}
        if message.get("method") == "Inspector.targetCrashed":
            raise ClosedError("вкладка браузера аварийно закрылась")
        return message

    def _keep(self, message):
        if "method" in message and message.get("sessionId") == self.session:
            self.events.append(message)
            del self.events[:-200]

    def call(self, method, params=None, timeout=60, browser=False):
        """Команда DevTools. browser=True — команда самому браузеру, иначе — вкладке."""
        if self.conn is None:
            raise ClosedError("браузер закрыт")
        self.next_id += 1
        my_id = self.next_id
        message = {"id": my_id, "method": method, "params": params or {}}
        if self.session and not browser:
            message["sessionId"] = self.session
        self.conn.send(json.dumps(message))
        deadline = time.time() + timeout
        while True:
            message = self._next(deadline, f"браузер не ответил вовремя ({method})")
            if message.get("id") == my_id:
                if "error" in message:
                    text = f"{method}: {message['error'].get('message', message['error'])}"
                    raise (NavigatedError if NAVIGATED_RE.search(text) else BrowserError)(text)
                return message.get("result", {})
            self._keep(message)

    def wait_event(self, names, timeout=60, match=None):
        """Дождаться события вкладки (одного из names; match — доп. условие). Уже пришедшие тоже считаются."""
        names = (names,) if isinstance(names, str) else tuple(names)

        def wanted(event):
            return event.get("method") in names and (match is None or match(event.get("params") or {}))

        for index, event in enumerate(self.events):
            if wanted(event):
                return self.events.pop(index)
        deadline = time.time() + timeout
        what = f"страница не загрузилась вовремя ({', '.join(names)})"
        while True:
            event = self._next(deadline, what)
            if wanted(event) and event.get("sessionId") == self.session:
                return event
            self._keep(event)

    def evaluate(self, expression, timeout=120, retries=0):
        """Выполнить скрипт на странице. Если страница в это время ушла на другой адрес,
        дождаться новой и повторить (retries раз), иначе — NavigatedError."""
        for attempt in range(retries + 1):
            try:
                return self._evaluate(expression, timeout)
            except NavigatedError:
                if attempt >= retries:
                    raise
                self.wait_ready(timeout=min(30, timeout))

    def _evaluate(self, expression, timeout):
        result = self.call("Runtime.evaluate", {"expression": expression, "awaitPromise": True,
                                                "returnByValue": True}, timeout=timeout)
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            text = str((details.get("exception") or {}).get("description") or details.get("text") or details)
            if NAVIGATED_RE.search(text):
                raise NavigatedError(f"страница ушла на другой адрес: {text[:200]}")
            raise BrowserError(f"ошибка скрипта на странице: {text[:300]}")
        return result.get("result", {}).get("value")

    def wait_ready(self, timeout=30):
        """Дождаться, пока новая страница разберёт HTML (после перехода на другой адрес)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self._evaluate("document.readyState", timeout=5) in ("interactive", "complete"):
                    return True
            except ClosedError:
                raise
            except (BrowserError, TimeoutError):  # в том числе NavigatedError: страница ещё меняется
                pass
            time.sleep(0.3)
        self.notes.append("новая страница не загрузилась вовремя")
        return False

    def open(self, url, settle=3.0, timeout=30, load_wait=10):
        """Открыть страницу. Ждём разбора HTML (DOMContentLoaded), затем до load_wait секунд
        — полной загрузки: если какая-то картинка или счётчик не грузится, читаем что есть."""
        self.events = [e for e in self.events if not str(e.get("method", "")).startswith("Page.")]
        result = self.call("Page.navigate", {"url": url}, timeout=timeout)
        if result.get("errorText") and "ERR_ABORTED" not in result["errorText"]:
            raise BrowserError(f"страница не открылась: {result['errorText']}")
        # События этой загрузки, а не прошлой страницы. Если страница сразу ушла дальше
        # (переадресация скриптом, проверка площадки), ждём уже новую: её номер — из frameNavigated.
        loaders, frame = {result.get("loaderId")}, result.get("frameId")
        events = ("Page.lifecycleEvent", "Page.frameNavigated")

        def stage(name):
            def match(params):
                info = params.get("frame") or {}
                if info.get("loaderId") and not info.get("parentId") and info.get("id") == frame:
                    loaders.add(info["loaderId"])
                    return False
                return params.get("name") == name and params.get("loaderId") in loaders
            return match

        deadline = time.time() + timeout
        try:
            if result.get("loaderId"):
                self.wait_event(events, timeout=timeout, match=stage("DOMContentLoaded"))
        except TimeoutError:
            self.notes.append(f"страница {url} не загрузилась за {timeout} с — читаю что есть")
        else:
            try:
                if result.get("loaderId"):
                    self.wait_event(events, match=stage("load"),
                                    timeout=max(0.5, min(load_wait, deadline - time.time())))
            except TimeoutError:
                self.notes.append(f"страница {url} не догрузилась (часть файлов не пришла) — читаю что есть")
        if settle:
            time.sleep(settle)  # страница догружает данные и проходит проверку площадки

    # --- чтение страниц ---

    def collect_cards(self, selector, rounds=40, timeout=150, wait_cards=30, max_pages=10, pause=0.7, stable_rounds=3,
                      resume=None):
        """Докрутить список до конца и собрать карточки товаров.

        Работает короткими шагами, а собранное хранится здесь: если страница сама
        ушла на другой адрес (проверка площадки закончилась, переадресация), уже
        прочитанное не теряется и чтение продолжается на новой странице. «Показать
        ещё» нажимается после чтения шага; если нажатие увело на другую страницу,
        это следующая страница (не больше max_pages), а на прежнем адресе кнопка
        больше не нажимается. Обычная ссылка на следующую страницу открывается.
        resume — страница, на которой остановился прошлый проход (его "next"): после
        первой страницы чтение продолжается с неё, а не со второй.
        -> {"cards": [{"href", "text", "inner"}], "api": [...], "title", "pages", "url",
            "next": следующая страница, до которой не дошли, или None}
        """
        cards, api = {}, {}
        title = url = following = None
        pages, navigations, scrolls, visited = 1, 0, 0, set()
        no_click = set()  # адреса, где «Показать ещё» уводит на другую страницу
        clicked_from = None  # адрес, где прошлый шаг нажал «Показать ещё»
        expect_nav = False  # нажатие уже засчитано как новая страница, страница уходит
        deadline = time.time() + timeout
        waiting_since = time.time()
        last, stable, errors = -1, 0, 0

        def page_change(origin):  # нажатие «Показать ещё» увело на другую страницу
            nonlocal pages, last, stable, scrolls, waiting_since
            pages += 1
            no_click.add(origin)
            last, stable, scrolls, waiting_since = -1, 0, 0, time.time()

        while True:
            left = deadline - time.time()
            if left <= 1:
                self.notes.append("карточки: время вышло")
                break
            script = JS_CARDS_ROUND % (json.dumps(selector), int(pause * 1000), json.dumps(pages < max_pages),
                                       json.dumps(sorted(no_click)))
            try:
                result = self._evaluate(script, timeout=min(30, left)) or {}
            except NavigatedError:
                if clicked_from is not None:
                    page_change(clicked_from)
                elif not expect_nav:
                    navigations += 1
                    if navigations > 10:
                        self.notes.append("карточки: страница всё время уходит на другой адрес")
                        break
                clicked_from, expect_nav = None, False
                try:
                    self.wait_ready(timeout=min(30, max(1, deadline - time.time())))
                except BrowserError as error:
                    self.notes.append(f"карточки: {error}")
                    break
                last, stable, waiting_since = -1, 0, time.time()
                continue
            except (TimeoutError, ClosedError) as error:
                self.notes.append(f"карточки: {error}")
                break
            except BrowserError as error:  # страница в переходном состоянии — ещё пара попыток
                errors += 1
                if errors >= 3:
                    self.notes.append(f"карточки: {error}")
                    break
                time.sleep(0.5)
                continue
            errors = 0
            if clicked_from is not None and (result.get("newDoc") or result.get("leaving")):
                page_change(clicked_from)  # уже на новой странице или уходит на неё
                expect_nav = bool(result.get("leaving"))
            elif not result.get("leaving"):
                expect_nav = False
            title, url = result.get("title"), result.get("url")
            clicked_from = url if result.get("clicked") else None
            if url:
                visited.add(url)
            fresh = result.get("cards") or []
            for card in fresh:
                if card.get("href"):
                    cards[card["href"]] = card
            for address in result.get("api") or []:
                api[address] = True
            following = result.get("next")
            count = result.get("count") or 0
            if not count:
                if time.time() - waiting_since > (wait_cards if pages == 1 else min(wait_cards, 10)):
                    break  # карточек так и нет — страница не та или не пустила
                continue
            scrolls += 1
            stable = stable + 1 if count == last and not fresh else 0
            last = count
            if stable < stable_rounds and scrolls < rounds:
                continue
            if following and pages == 1 and resume and resume not in visited and _same_site(resume, url):
                following = resume  # прошлый проход дошёл досюда — продолжаем с этого места
            if following and following not in visited and pages < max_pages:
                visited.add(following)
                pages += 1
                try:
                    self.open(following, settle=0)
                except (BrowserError, TimeoutError) as error:  # собранное раньше остаётся
                    self.notes.append(f"следующая страница: {error}")
                    break
                last, stable, scrolls, waiting_since = -1, 0, 0, time.time()
                continue
            break
        following = following if following and following not in visited else None
        return {"cards": list(cards.values()), "api": list(api)[:200], "title": title, "pages": pages, "url": url,
                "next": following}

    def fetch_many(self, urls, pause=1.0, timeout=15, deadline=60, stop_on=(403, 429, 503)):
        """Запросы изнутри страницы (с её cookies) — по одному, с паузой.

        На первом отказе площадки (stop_on), по истечении deadline секунд или если
        браузер закрылся — остановка; то, что уже прочитано, возвращается. Запрос
        длиннее timeout секунд прерывается. HTML приходит очищенным от стилей,
        картинок и скриптов (данные страницы остаются) и укороченным.
        -> ({url: (код, текст)}, причина остановки или None)
        """
        out, stop, total = {}, None, 0
        end = time.time() + deadline
        for url in urls:
            if STREAM_URL_RE.search(url):
                continue  # подписка на события: не кончается, товаров в ней нет
            if out and pause:
                time.sleep(pause)
            left = end - time.time()
            if left < 1:
                stop = "время вышло"
                break
            wait = min(timeout, left)
            options = {"timeout": int(wait * 1000), "maxHtml": MAX_PAGE_CHARS, "maxData": MAX_DATA_CHARS}
            try:
                value = self.evaluate(JS_FETCH % (json.dumps(url), json.dumps(options)), timeout=wait + 10, retries=1)
            except NavigatedError:
                out[url] = (0, "страница ушла на другой адрес")
                continue
            except (TimeoutError, ClosedError) as error:
                stop = str(error)
                break
            except BrowserError as error:
                out[url] = (0, str(error))
                continue
            status, text = (value or [0, ""])[:2]
            out[url] = (int(status or 0), str(text or ""))
            total += len(out[url][1])
            if status in stop_on:
                stop = f"HTTP {status}"
                break
            if total > MAX_TOTAL_CHARS:
                stop = "слишком много данных"
                break
        return out, stop

    # --- закрытие ---

    def close(self):
        """Закрыть браузер (вежливо, затем принудительно) и удалить его профиль."""
        conn = self.conn
        if conn is not None:
            if self.process is not None and self.process.poll() is None:
                try:
                    self.call("Browser.close", timeout=3, browser=True)
                except Exception:
                    pass
            self.conn = None
            conn.close()
        with self._lock:
            process, self.process = self.process, None
        if process is not None:
            try:
                process.wait(timeout=5)
            except Exception:
                _kill(process)
        _forget(self)
        self._remove_profile(tries=5)

    def kill(self):
        """Сразу завершить браузер и удалить профиль (выход из программы, другой поток)."""
        with self._lock:
            process, self.process = self.process, None
        if process is not None:
            _kill(process)
        _forget(self)
        self._remove_profile(tries=3)

    def _remove_profile(self, tries):
        for attempt in range(tries):  # Windows может держать файлы профиля ещё пару секунд
            shutil.rmtree(self.profile, ignore_errors=True)
            if not os.path.exists(self.profile):
                break
            if attempt + 1 < tries:
                time.sleep(0.5)


def _kill(process):
    try:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
    except Exception:
        pass


def _forget(browser):
    with _LIVE_LOCK:
        _LIVE.discard(browser)


def _same_site(one, other):
    parts = [urllib.parse.urlsplit(str(u or "")) for u in (one, other)]
    return parts[0].scheme in ("http", "https") and parts[0][:2] == parts[1][:2]


def local_json(url, timeout=5):
    """JSON с адреса DevTools на 127.0.0.1 — всегда напрямую: прокси системы
    (в Windows — из настроек) не знает этого адреса и запрос не пропустит."""
    with _DIRECT.open(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def live_browsers():
    with _LIVE_LOCK:
        return list(_LIVE)


def close_all():
    """Сразу закрыть все открытые скрытые браузеры (остановка опроса, выход из программы)."""
    for browser in live_browsers():
        browser.kill()


def _at_exit():
    with _LIVE_LOCK:
        _STATE["exiting"] = True  # поток опроса ещё жив: новых браузеров не запускать
    close_all()


atexit.register(_at_exit)


def sweep_stale_profiles(max_age=STALE_PROFILE_AGE, folder=None):
    """Удалить временные профили, оставшиеся после аварийного выхода (один раз за запуск)."""
    with _LIVE_LOCK:
        if _STATE["swept"] and folder is None:
            return 0
        _STATE["swept"] = True
    folder = folder or tempfile.gettempdir()
    try:
        names = [name for name in os.listdir(folder) if name.startswith(PROFILE_PREFIX)]
    except OSError:
        return 0
    removed = 0
    now = time.time()
    for name in names:
        path = os.path.join(folder, name)
        try:
            if os.path.islink(path) or not os.path.isdir(path) or now - os.path.getmtime(path) < max_age:
                continue
        except OSError:
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed += not os.path.exists(path)
    return removed


# --- Windows: браузер живёт не дольше программы -------------------------------------

def _job_limit_structure():
    """JOBOBJECT_EXTENDED_LIMIT_INFORMATION для ctypes (поля — как в winnt.h)."""
    import ctypes

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    return ExtendedLimits


def _windows_job():
    """Одно задание Windows на все браузеры программы: KILL_ON_JOB_CLOSE закрывает их,
    когда закрывается последний дескриптор задания — то есть вместе с программой,
    даже если её окно закрыли или процесс завершили. Дескриптор не закрывается."""
    if _STATE["job"] is None:
        _STATE["job"] = 0
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _job_limit_structure()()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # Extended
            kernel32.CloseHandle(job)
            return None
        _STATE["job"] = (kernel32, job)
    return _STATE["job"] or None


def _windows_job_assign(process):
    """Включить процесс браузера в задание. Любая ошибка — не помеха: тогда браузер
    закроют close_all()/atexit, а профиль — уборка при следующем запуске."""
    if os.name != "nt":
        return False
    try:
        job = _windows_job()
        if not job:
            return False
        kernel32, handle = job
        return bool(kernel32.AssignProcessToJobObject(handle, int(process._handle)))
    except Exception:
        return False


# --- скрипты для страницы ---------------------------------------------------------

# Подсказки клиента (только на «безопасной» странице, поэтому — chrome://version).
JS_UA_DATA = r"""(async () => {
  if (!/^(chrome|edge):$/.test(location.protocol) || !navigator.userAgentData) return null;
  const d = await navigator.userAgentData.getHighEntropyValues(
    ['architecture', 'bitness', 'model', 'platformVersion', 'fullVersionList', 'wow64']);
  return JSON.parse(JSON.stringify(d));
})()"""

# Один шаг чтения рынка: докрутить вниз, прочитать новые и изменившиеся карточки (ссылка +
# текст), и только потом нажать «Показать ещё» (кнопку или ссылку на эту же страницу) —
# уже после ответа: если нажатие уведёт на другую страницу, прочитанное не пропадёт.
# Карточка — самый большой блок вокруг ссылки, где все ссылки на товар ведут на один
# товар (картинка и название часто — две отдельные ссылки, цена — вне обеих). Текст — как
# innerText (блоки — с новой строки, строчные части склеиваются: "от <i>231</i><i>,71</i> ₽"
# остаётся "от 231,71 ₽", "<b>Chroma</b> Seer" — "Chroma Seer"), но на стыке разных
# элементов число не приклеивается к числу или слову: "<i>114</i><b>от 230 ₽</b>" —
# "114" и "от 230 ₽" на разных строках, а не "114от 230 ₽".
# newDoc — первый шаг на этом документе (страница сменилась), leaving — страница уже уходит.
JS_CARDS_ROUND = r"""(async (selector, wait, click, noClick) => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const here = location.href.split('#')[0];
  const newDoc = !window.__mm2Cards;
  const seen = window.__mm2Cards || (window.__mm2Cards = new Map());
  const boxes = window.__mm2Boxes || (window.__mm2Boxes = new Map());
  if (newDoc) {
    for (const name of ['beforeunload', 'pagehide']) addEventListener(name, () => { window.__mm2Leaving = true; });
  }
  window.scrollTo(0, document.documentElement.scrollHeight);
  await sleep(wait);
  const skip = /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|svg)$/;
  const kind = (c) => /[0-9]/.test(c) ? 'd' : /[A-Za-z]/.test(c) ? 'l' : /[А-Яа-яЁё]/.test(c) ? 'c' : '';
  const textOf = (root) => {
    let out = '', parent = null;
    const visit = (node, visible) => {
      if (node.nodeType === 3) {
        const t = node.nodeValue.replace(/\s+/g, ' ');
        if (!visible || !t) return;
        const a = kind(out.slice(-1)), b = kind(t[0]);
        if (parent !== node.parentElement && a && b && (a === 'd' || b === 'd' || a !== b)) out += '\n';
        out += t;
        parent = node.parentElement;
        return;
      }
      if (node.nodeType !== 1 || skip.test(node.tagName)) return;
      if (node.tagName === 'BR') { out += '\n'; return; }
      const style = getComputedStyle(node);
      if (style.display === 'none') return;
      const block = !/^(inline|contents|ruby)/.test(style.display);
      if (block) out += '\n';
      for (const child of node.childNodes) visit(child, style.visibility === 'visible');
      if (block) out += '\n';
    };
    visit(root, true);
    return out.split('\n').map((line) => line.replace(/ +/g, ' ').trim()).filter(Boolean).join('\n');
  };
  const single = (el, href) => {
    for (const x of el.querySelectorAll(selector)) if (x.href !== href) return false;
    return true;
  };
  const fresh = [];
  const hrefs = new Set();
  for (const a of document.querySelectorAll(selector)) {
    const href = a.href;
    if (!href || hrefs.has(href)) continue;
    hrefs.add(href);
    let box = boxes.get(href);
    if (!box || !box.isConnected || !box.contains(a) || !single(box, href)) {
      box = a;
      while (box.parentElement && box.parentElement !== document.body && single(box.parentElement, href))
        box = box.parentElement;
      boxes.set(href, box);
    }
    const text = textOf(box).slice(0, 1500);
    if (seen.get(href) !== text) {
      seen.set(href, text);
      fresh.push({ href, text, inner: (box.innerText || '').slice(0, 1500) });
    }
  }
  const api = performance.getEntriesByType('resource')
    .filter((e) => e.initiatorType === 'fetch' || e.initiatorType === 'xmlhttprequest')
    .map((e) => e.name);
  const leaving = !!window.__mm2Leaving;
  const label = /показать ещё|показать еще|загрузить ещё|загрузить еще|load more|show more/i;
  const inPlace = (el) => {
    if (el.tagName !== 'A') return true;
    const href = (el.getAttribute('href') || '').trim();
    return !href || href[0] === '#' || /^javascript:/i.test(href) || el.href.split('#')[0] === here;
  };
  let next = null, clicked = false;
  for (const el of leaving ? [] : document.querySelectorAll('button, [role="button"], a')) {
    const caption = (el.textContent || '').trim();
    if (caption.length > 60 || !label.test(caption) || el.offsetParent === null || el.disabled) continue;
    if (inPlace(el)) {
      if (click && !noClick.includes(here)) {
        clicked = true;
        setTimeout(() => { try { el.click(); } catch (e) {} }, 50);
      }
      next = null;
      break;
    }
    if (!next && el.href && el.origin === location.origin) next = el.href;
  }
  return { cards: fresh, count: hrefs.size, api: [...new Set(api)].slice(0, 200), title: document.title,
           next, url: here, clicked, newDoc, leaving };
})(%s, %d, %s, %s)"""

# Один запрос изнутри страницы: с её cookies, с пределом времени; HTML — без стилей,
# картинок и обычных скриптов (данные страницы — JSON-LD, __NEXT_DATA__ и т. п. — остаются).
JS_FETCH = r"""(async (url, o) => {
  const c = new AbortController();
  const timer = setTimeout(() => c.abort(), o.timeout);
  try {
    const r = await fetch(url, { credentials: 'include', signal: c.signal });
    const type = r.headers.get('content-type') || '';
    if (/event-stream/i.test(type)) { c.abort(); return [r.status, '']; }
    let text = await r.text();
    if (/html/i.test(type) || /^\s*</.test(text.slice(0, 200))) {
      text = text.replace(/<style\b[\s\S]*?<\/style>/gi, '').replace(/<svg\b[\s\S]*?<\/svg>/gi, '')
        .replace(/<!--[\s\S]*?-->/g, '')
        .replace(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi, (m, attrs, body) =>
          /json/i.test(attrs) || /__next_f|__NEXT_DATA__|__NUXT|window\.__[A-Z_]+__/.test(body)
            ? m : '<script' + attrs + '></script>');
      return [r.status, text.slice(0, o.maxHtml)];
    }
    return [r.status, text.slice(0, o.maxData)];
  } catch (e) {
    return [0, c.signal.aborted ? 'запрос прерван: нет ответа вовремя' : String(e)];
  } finally { clearTimeout(timer); }
})(%s, %s)"""
