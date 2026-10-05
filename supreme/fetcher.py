"""Загрузка страниц supremevalues.com."""

import http.client
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import parser

BASE_URL = "https://supremevalues.com/mm2/"
HOME_URL = "https://supremevalues.com/mm2"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class FetchError(Exception):
    pass


class BlockedError(FetchError):
    """Сайт вернул защитную страницу вместо данных."""


class NetworkError(FetchError):
    """Нет соединения с сайтом (нет интернета, сайт недоступен)."""


_CONTENT_RE = re.compile(r"(Value|Range)\s*[-–—:]|Last\s+Updated", re.I)
BLOCK_STATUSES = (403, 429, 503)


def looks_blocked(page_html):
    """Защитная страница вместо сайта. Ссылка на скрипт защиты бывает и на
    нормальной странице, поэтому блокировкой считается только страница без данных."""
    if "_Incapsula_Resource" not in page_html and "Request unsuccessful" not in page_html:
        return False
    if parser.parse_popup(page_html):
        return False
    return not _CONTENT_RE.search(parser.html_to_text(page_html))


def _decode(raw, charset):
    try:
        return raw.decode(charset or "utf-8", "replace")
    except LookupError:  # неизвестная кодировка в заголовке
        return raw.decode("utf-8", "replace")


def fetch_plain(url, timeout=30, attempts=3):
    last_error = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                page_html = _decode(response.read(), response.headers.get_content_charset())
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise FetchError(f"страница не найдена (404): {url}") from error
            try:
                body = _decode(error.read(), error.headers.get_content_charset() if error.headers else None)
            except Exception:
                body = ""
            if error.code in BLOCK_STATUSES or looks_blocked(body):
                raise BlockedError(f"сайт показал защитную проверку (HTTP {error.code}): {url}") from error
            last_error = error
        except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
            last_error = error
        else:
            if looks_blocked(page_html):
                raise BlockedError(f"сайт показал защитную проверку: {url}")
            return page_html
        if attempt + 1 < attempts:
            time.sleep(2 * (attempt + 1))
    if isinstance(last_error, urllib.error.HTTPError):
        raise FetchError(f"сайт ответил ошибкой HTTP {last_error.code}: {url}")
    raise NetworkError(f"нет соединения с сайтом ({last_error}): {url}")


class BrowserFetcher:
    """Загрузка через Chromium (Playwright), если сайт не отдаёт страницу напрямую.

    Нужен только если обычная загрузка заблокирована:
        pip install playwright
        python -m playwright install chromium
    """

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None

    @staticmethod
    def available():
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            return False
        return True

    def fetch(self, url):
        from playwright.sync_api import sync_playwright

        if self._context is None:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch()
            self._context = self._browser.new_context(user_agent=HEADERS["User-Agent"])
        page = self._context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_function(
                    "document.body && /Value\\s*[-–:]/.test(document.body.innerText)", timeout=30000
                )
            except Exception:
                pass  # на главной странице карточек нет — берём то, что загрузилось
            page_html = page.content()
        finally:
            page.close()
        if looks_blocked(page_html):
            raise BlockedError(f"сайт показал защитную проверку даже браузеру: {url}")
        return page_html

    def close(self):
        for closer in (
            self._context and self._context.close,
            self._browser and self._browser.close,
            self._playwright and self._playwright.stop,
        ):
            if closer:
                try:
                    closer()
                except Exception:
                    pass
        self._playwright = self._browser = self._context = None


def find_system_browser():
    """Edge или Chrome, установленные на компьютере (Edge есть в любой Windows 10/11)."""
    candidates = []
    if os.name == "nt":
        for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            base = os.environ.get(variable)
            if base:
                candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
                candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
        candidates.append(Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"))
    for name in ("msedge", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


class SystemBrowserFetcher:
    """Загрузка через Edge/Chrome в скрытом режиме: браузер сам проходит проверку
    сайта и возвращает готовую страницу. Ничего устанавливать не нужно."""

    def __init__(self, executable):
        self.executable = executable

    def fetch(self, url, timeout=120):
        profile = tempfile.mkdtemp(prefix="mm2values-browser-")
        command = [
            self.executable,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            f"--user-data-dir={profile}",
            f"--user-agent={HEADERS['User-Agent']}",
            "--virtual-time-budget=20000",
            "--dump-dom",
            url,
        ]
        if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
            command.insert(1, "--no-sandbox")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            result = subprocess.run(
                command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=timeout, creationflags=flags,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise FetchError(f"браузер не смог открыть {url}: {error}") from error
        finally:
            shutil.rmtree(profile, ignore_errors=True)
        page_html = result.stdout.decode("utf-8", "replace")
        if result.returncode != 0 or "<" not in page_html:
            raise FetchError(f"браузер не смог открыть {url} (код {result.returncode})")
        if looks_blocked(page_html):
            raise BlockedError(f"сайт показал защитную проверку даже браузеру: {url}")
        return page_html

    def close(self):
        pass


class Fetcher:
    """Сначала обычная загрузка. Если сайт показывает защитную проверку —
    через браузер: Playwright (если установлен), затем Edge/Chrome с компьютера."""

    def __init__(self):
        self._browsers = None
        self._prefer_browser = False

    def _browser_list(self):
        if self._browsers is None:
            self._browsers = []
            if BrowserFetcher.available():
                self._browsers.append(BrowserFetcher())
            executable = find_system_browser()
            if executable:
                self._browsers.append(SystemBrowserFetcher(executable))
        return self._browsers

    def __call__(self, url):
        if not self._prefer_browser:
            try:
                return fetch_plain(url)
            except BlockedError:
                self._prefer_browser = True  # дальше сразу через браузер
        errors = []
        for browser in self._browser_list():
            try:
                return browser.fetch(url)
            except FetchError as error:
                errors.append(str(error))
            except Exception as error:  # сбой самого браузера — пробуем следующий
                errors.append(f"{type(error).__name__}: {error}")
        if not self._browser_list():
            errors.append("на компьютере не найден Edge или Chrome")
        raise BlockedError(
            "сайт показывает защитную проверку вместо страницы ("
            + "; ".join(errors)
            + "). Перенесите цены через вкладку «Импорт»."
        )

    def close(self):
        for browser in self._browsers or []:
            browser.close()
