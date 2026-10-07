"""Скрытый Edge/Chrome по протоколу DevTools — стандартной библиотекой Python.

Нужен для площадок, которые рисуют список товаров скриптом или не пускают
обычные запросы: браузер открывает страницу как обычный посетитель, а программа
читает из неё готовые карточки и выполняет запросы изнутри страницы.
"""

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

from . import fetcher


class BrowserError(Exception):
    pass


class WebSocket:
    """Минимальный клиент WebSocket (RFC 6455) для соединения с браузером на 127.0.0.1."""

    def __init__(self, url, timeout=30):
        parts = urllib.parse.urlsplit(url)
        self.sock = socket.create_connection((parts.hostname, parts.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = parts.path + (("?" + parts.query) if parts.query else "")
        request = (f"GET {path} HTTP/1.1\r\nHost: {parts.hostname}:{parts.port}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise BrowserError("браузер не открыл соединение")
            response += chunk
        head, self.buffer = response.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise BrowserError("браузер не открыл соединение: " + head.decode("latin-1")[:200])

    def _read(self, size):
        while len(self.buffer) < size:
            chunk = self.sock.recv(1 << 16)
            if not chunk:
                raise BrowserError("соединение с браузером закрыто")
            self.buffer += chunk
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def _frame(self, opcode, payload):
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", length)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def send(self, text):
        self._frame(0x1, text.encode("utf-8"))

    def recv(self, timeout):
        self.sock.settimeout(max(0.1, timeout))
        message = b""
        try:
            while True:
                first, second = self._read(2)
                opcode, length = first & 0x0F, second & 0x7F
                if length == 126:
                    length = struct.unpack(">H", self._read(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", self._read(8))[0]
                mask = self._read(4) if second & 0x80 else None
                data = self._read(length)
                if mask:
                    data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
                if opcode == 0x8:
                    raise BrowserError("браузер закрыл соединение")
                if opcode == 0x9:  # ping -> pong
                    self._frame(0xA, data)
                    continue
                if opcode == 0xA:
                    continue
                message += data
                if first & 0x80:
                    return message.decode("utf-8", "replace")
        except socket.timeout as error:
            raise TimeoutError("браузер не ответил вовремя") from error

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Browser:
    """Скрытый Edge/Chrome. Использовать через `with Browser(...) as browser:`."""

    def __init__(self, executable=None, timeout=45):
        self.executable = executable or fetcher.find_system_browser()
        if not self.executable:
            raise BrowserError("на компьютере не найден Edge или Chrome")
        self.profile = tempfile.mkdtemp(prefix="mm2values-cdp-")
        self.process = None
        self.ws = None
        self.next_id = 0
        self.events = []
        command = [
            self.executable, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--disable-extensions", "--mute-audio", "--window-size=1366,900", "--lang=ru-RU",
            f"--user-agent={fetcher.HEADERS['User-Agent']}",
            "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
            f"--user-data-dir={self.profile}", "about:blank",
        ]
        if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
            command.insert(1, "--no-sandbox")
        try:
            self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._connect(timeout)
        except Exception:
            self.close()
            raise

    def _connect(self, timeout):
        port_file = os.path.join(self.profile, "DevToolsActivePort")
        deadline = time.time() + timeout
        port = None
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise BrowserError("браузер не запустился")
            try:
                with open(port_file, encoding="utf-8") as file:
                    port = file.read().split()[0]
                break
            except (OSError, IndexError):
                time.sleep(0.2)
        if not port:
            raise BrowserError("браузер не запустился вовремя")
        page = None
        while time.time() < deadline and page is None:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
                    targets = json.loads(response.read().decode("utf-8"))
                page = next((t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
            except (OSError, ValueError):
                pass
            if page is None:
                time.sleep(0.3)
        if page is None:
            raise BrowserError("в браузере не открылась страница")
        self.ws = WebSocket(page["webSocketDebuggerUrl"], timeout=timeout)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def call(self, method, params=None, timeout=60):
        self.next_id += 1
        my_id = self.next_id
        self.ws.send(json.dumps({"id": my_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise TimeoutError(f"браузер не ответил вовремя ({method})")
            message = json.loads(self.ws.recv(left))
            if message.get("id") == my_id:
                if "error" in message:
                    raise BrowserError(f"{method}: {message['error'].get('message', message['error'])}")
                return message.get("result", {})
            if "method" in message:
                self.events.append(message)
                del self.events[:-200]

    def wait_event(self, name, timeout=60):
        for index, event in enumerate(self.events):
            if event.get("method") == name:
                return self.events.pop(index)
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise TimeoutError(f"страница не загрузилась вовремя ({name})")
            event = json.loads(self.ws.recv(left))
            if event.get("method") == name:
                return event
            if "method" in event:
                self.events.append(event)
                del self.events[:-200]

    def evaluate(self, expression, timeout=120):
        result = self.call("Runtime.evaluate", {"expression": expression, "awaitPromise": True,
                                                "returnByValue": True}, timeout=timeout)
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            text = (details.get("exception") or {}).get("description") or details.get("text") or str(details)
            raise BrowserError(f"ошибка скрипта на странице: {str(text)[:300]}")
        return result.get("result", {}).get("value")

    def open(self, url, settle=3.0, timeout=60):
        self.call("Page.enable")
        self.call("Page.navigate", {"url": url}, timeout=timeout)
        self.wait_event("Page.loadEventFired", timeout=timeout)
        time.sleep(settle)  # страница догружает данные и проходит проверку площадки

    def close(self):
        if self.ws is not None:
            try:
                self.call("Browser.close", timeout=5)
            except Exception:
                pass
            self.ws.close()
            self.ws = None
        if self.process is not None:
            try:
                self.process.wait(timeout=10)
            except Exception:
                self.process.kill()
                try:
                    self.process.wait(timeout=5)
                except Exception:
                    pass
            self.process = None
        for _ in range(5):  # Windows может держать файлы профиля ещё пару секунд
            shutil.rmtree(self.profile, ignore_errors=True)
            if not os.path.exists(self.profile):
                break
            time.sleep(0.5)


# Скрипт для страницы магазина: докрутить список до конца, собрать карточки
# товаров (ссылка + текст карточки) и адреса запросов, которые сделала страница.
JS_COLLECT_CARDS = r"""(async (selector, maxRounds) => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  let last = -1, stable = 0;
  for (let i = 0; i < maxRounds && stable < 3; i++) {
    window.scrollTo(0, document.documentElement.scrollHeight);
    await sleep(700);
    const more = [...document.querySelectorAll('button, a')].find((b) =>
      /показать ещё|показать еще|загрузить ещё|load more|show more/i.test(b.textContent || '') && b.offsetParent);
    if (more) { try { more.click(); } catch (e) {} await sleep(700); }
    const n = document.querySelectorAll(selector).length;
    if (n === last) stable++; else { stable = 0; last = n; }
  }
  const cards = [];
  const seen = new Set();
  for (const a of document.querySelectorAll(selector)) {
    const href = a.href;
    if (!href || seen.has(href)) continue;
    seen.add(href);
    let node = a;
    while (node.parentElement && node.parentElement !== document.body &&
           node.parentElement.querySelectorAll(selector).length <= 1) node = node.parentElement;
    cards.push({ href, text: (node.innerText || a.innerText || '').slice(0, 600) });
  }
  const api = performance.getEntriesByType('resource')
    .filter((e) => e.initiatorType === 'fetch' || e.initiatorType === 'xmlhttprequest')
    .map((e) => e.name);
  return { cards, api: [...new Set(api)].slice(0, 200), title: document.title };
})(%s, %d)"""

JS_FETCH_MANY = r"""(async (urls) => {
  const out = {};
  for (let i = 0; i < urls.length; i += 4) {
    await Promise.all(urls.slice(i, i + 4).map(async (u) => {
      try {
        const r = await fetch(u, { credentials: 'include' });
        const text = await r.text();
        out[u] = [r.status, text.slice(0, 3000000)];
      } catch (e) { out[u] = [0, String(e)]; }
    }));
  }
  return out;
})(%s)"""
