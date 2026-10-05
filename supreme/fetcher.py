"""Загрузка страниц supremevalues.com."""

import time
import urllib.error
import urllib.request

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


def looks_blocked(page_html):
    return "_Incapsula_Resource" in page_html or "Request unsuccessful" in page_html


def fetch_plain(url, timeout=30, attempts=3):
    last_error = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                page_html = response.read().decode(charset, "replace")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise FetchError(f"страница не найдена (404): {url}") from error
            last_error = error
        except (urllib.error.URLError, OSError) as error:
            last_error = error
        else:
            if looks_blocked(page_html):
                raise BlockedError(f"сайт показал защитную проверку: {url}")
            return page_html
        if attempt + 1 < attempts:
            time.sleep(2 * (attempt + 1))
    raise FetchError(f"не удалось открыть {url}: {last_error}")


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


class Fetcher:
    """Сначала обычная загрузка; если сайт блокирует — браузер (если установлен)."""

    def __init__(self):
        self.browser = BrowserFetcher() if BrowserFetcher.available() else None

    def __call__(self, url):
        try:
            return fetch_plain(url)
        except BlockedError:
            if self.browser is None:
                raise BlockedError(
                    "сайт блокирует прямую загрузку. Установите браузер для программы: "
                    "pip install playwright && python -m playwright install chromium"
                ) from None
            return self.browser.fetch(url)

    def close(self):
        if self.browser:
            self.browser.close()
