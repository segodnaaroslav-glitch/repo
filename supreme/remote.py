"""Доступ к программе с другого компьютера по паролю: программа работает на одном компьютере
(например, на выделенном сервере, где она круглосуточно следит за площадками), а окно
открывается на другом — по ссылке http://<адрес>:<порт>/, после ввода пароля.

Пока доступ выключен, программа слушает только этот компьютер (127.0.0.1), как раньше.
Включённый доступ действует после перезапуска программы: она слушает сеть, но с других
компьютеров пускает только после ввода пароля (на этом компьютере — без пароля).
Пароль хранится как соль и PBKDF2-хэш; вход запоминается в браузере (подписанная cookie на
30 дней); смена пароля разлогинивает все браузеры. Подбор пароля ограничен (LoginGuard).
"""

import hashlib
import hmac
import json
import secrets
import socket
import threading
import time
from pathlib import Path

SETTINGS = "remote.json"
COOKIE = "mm2session"
SESSION_DAYS = 30
MIN_PASSWORD = 6
_ITERATIONS = 200_000


def _path(data_dir):
    return Path(data_dir) / SETTINGS


def load(data_dir):
    """{"enabled", "salt", "hash", "secret"} — пустые строки, пока пароль не задан."""
    try:
        saved = json.loads(_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    settings = {field: str(saved.get(field) or "") for field in ("salt", "hash", "secret")}
    settings["enabled"] = bool(saved.get("enabled")) and has_password(settings)
    return settings


def has_password(settings):
    return bool(settings.get("salt") and settings.get("hash") and settings.get("secret"))


def save(data_dir, settings):
    path = _path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({field: settings.get(field) for field in ("enabled", "salt", "hash", "secret")}),
                   encoding="utf-8")
    tmp.replace(path)


def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), bytes.fromhex(salt), _ITERATIONS).hex()


def set_password(data_dir, password):
    """Новый пароль (и новая подпись входа: все браузеры входят заново). ValueError — слишком короткий."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise ValueError(f"Пароль — не короче {MIN_PASSWORD} символов")
    settings = load(data_dir)
    settings["salt"] = secrets.token_hex(16)
    settings["hash"] = _hash(password, settings["salt"])
    settings["secret"] = secrets.token_hex(32)
    save(data_dir, settings)
    return settings


def set_enabled(data_dir, enabled):
    """Включить или выключить доступ. ValueError — включить без пароля нельзя."""
    settings = load(data_dir)
    if enabled and not has_password(settings):
        raise ValueError("Сначала задайте пароль")
    settings["enabled"] = bool(enabled)
    save(data_dir, settings)
    return settings


def check_password(settings, password):
    if not has_password(settings) or not isinstance(password, str):
        return False
    return hmac.compare_digest(_hash(password, settings["salt"]), settings["hash"])


def _sign(settings, issued):
    return hmac.new(bytes.fromhex(settings["secret"]), f"v1.{issued}".encode(), hashlib.sha256).hexdigest()


def make_session(settings, now=None):
    issued = int(now if now is not None else time.time())
    return f"v1.{issued}.{_sign(settings, issued)}"


def check_session(settings, token, now=None):
    """Вход, запомненный в браузере: подпись верна и ему не больше SESSION_DAYS дней."""
    if not has_password(settings) or not isinstance(token, str):
        return False
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "v1" or not parts[1].isdigit():
        return False
    issued = int(parts[1])
    age = (now if now is not None else time.time()) - issued
    if age < -300 or age > SESSION_DAYS * 86400:
        return False
    return hmac.compare_digest(_sign(settings, issued), parts[2])


class LoginGuard:
    """Защита от подбора пароля: после FREE неудачных попыток с одного адреса — пауза, которая
    удваивается с каждой следующей ошибкой (до часа); удачный вход сбрасывает счёт."""

    FREE = 5
    FIRST_PAUSE = 60
    MAX_PAUSE = 3600

    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = {}  # адрес -> (ошибок подряд, заблокирован до)

    def wait(self, address):
        """Сколько секунд ещё нельзя пробовать с этого адреса (0 — можно)."""
        with self._lock:
            _, until = self._failures.get(address, (0, 0))
            return max(0, int(until - self._clock() + 0.999))

    def failed(self, address):
        with self._lock:
            count, _ = self._failures.get(address, (0, 0))
            count += 1
            until = 0
            if count >= self.FREE:
                until = self._clock() + min(self.MAX_PAUSE, self.FIRST_PAUSE * 2 ** (count - self.FREE))
            self._failures[address] = (count, until)
            if len(self._failures) > 10000:  # не копить адреса бесконечно
                self._failures.pop(next(iter(self._failures)))

    def passed(self, address):
        with self._lock:
            self._failures.pop(address, None)


def addresses():
    """IPv4-адреса этого компьютера в сети (без 127.x): первым — тот, через который он выходит
    в интернет (на выделенном сервере это обычно его внешний адрес)."""
    found = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 53))  # пакет не отправляется: только выбор сетевой карты
            found.append(probe.getsockname()[0])
    except OSError:
        pass
    try:
        found += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    out = []
    for address in found:
        if address and not address.startswith("127.") and address != "0.0.0.0" and address not in out:
            out.append(address)
    return out


def links(port):
    """Адреса программы для другого компьютера (по одному на каждый адрес этого)."""
    return [f"http://{address}:{port}/" for address in addresses()]


LOGIN_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>MM2 Values — вход</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0f141d;
color:#e6e9ef;font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
form{width:min(340px,calc(100vw - 32px));padding:28px;border:1px solid #2a3344;border-radius:14px;background:#161d2a}
h1{margin:0 0 4px;font-size:20px}p{margin:0 0 18px;color:#8b93a6;font-size:13px}
input{box-sizing:border-box;width:100%;padding:10px 12px;border:1px solid #2a3344;border-radius:8px;
background:#0f141d;color:inherit;font:inherit}
button{margin-top:12px;width:100%;padding:10px;border:0;border-radius:8px;background:#7c8cff;color:#fff;
font:inherit;font-weight:600;cursor:pointer}button:disabled{opacity:.6}
#msg{min-height:20px;margin-top:10px;color:#ff8a8a;font-size:13px}
</style></head><body>
<form id="f"><h1>MM2 Values</h1><p>Программа на другом компьютере. Введите пароль, заданный в ней.</p>
<input id="p" type="password" autocomplete="current-password" placeholder="Пароль" aria-label="Пароль" required autofocus>
<button id="b" type="submit">Войти</button><div id="msg" role="alert"></div></form>
<script>
document.getElementById('f').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = document.getElementById('b'), msg = document.getElementById('msg');
  button.disabled = true;
  msg.textContent = '';
  try {
    const response = await fetch('/api/login', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password: document.getElementById('p').value})});
    if (response.ok) { location.replace('/'); return; }
    const body = await response.json().catch(() => ({}));
    msg.textContent = body.error || ('Ошибка ' + response.status);
  } catch (error) {
    msg.textContent = 'Нет связи с программой';
  }
  button.disabled = false;
});
</script></body></html>"""
