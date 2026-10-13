"""Отчёт о DreamPets: что площадка на самом деле показывает и отдаёт — для точной настройки.

Скрытый Edge/Chrome открывает рынок DreamPets как обычный посетитель и записывает:
- карточки товаров (текст и разметку) и что программа из них прочитала;
- ответы самой площадки с данными (её API: адрес и JSON);
- несколько страниц товаров (текст и что программа прочитала);
- что программа сейчас показывает для этих же товаров.

Всё сохраняется одним файлом в папку debug\\dreampets рядом с ценами.
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path

from . import cdp, dreampets, store

BODY_LIMIT = 150_000  # символов одного ответа площадки
BODIES_MAX = 40
CARDS_MAX = 80
_STATIC_RE = re.compile(r"\.(js|css|png|jpe?g|webp|gif|svg|ico|woff2?|ttf|mp4|webm)(\?|$)", re.I)

# Разметка первых карточек: ближайший к ссылке блок, где только этот товар.
JS_CARD_HTML = r"""(() => {
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/mm2/product/"]')) {
    if (seen.has(a.href)) continue;
    seen.add(a.href);
    let node = a;
    while (node.parentElement && node.parentElement !== document.body &&
           new Set([...node.parentElement.querySelectorAll('a[href*="/mm2/product/"]')].map((x) => x.href)).size <= 1) {
      node = node.parentElement;
    }
    out.push({href: a.href, html: node.outerHTML.slice(0, 6000)});
    if (out.length >= 6) break;
  }
  return out;
})()"""


def _responses(browser, taken):
    """Ответы площадки с данными (JSON / XHR / fetch), ещё не записанные."""
    out = []
    for response in list(browser.responses or []):
        info = response.get("response") or {}
        url = str(info.get("url") or "")
        kind = str(response.get("type") or "")
        mime = str(info.get("mimeType") or "")
        if url in taken or not url.startswith("http") or _STATIC_RE.search(url.split("#")[0]):
            continue
        if kind not in ("XHR", "Fetch") and "json" not in mime:
            continue
        taken.add(url)
        if len(taken) > BODIES_MAX:
            break
        entry = {"url": url, "status": info.get("status"), "type": kind, "mime": mime}
        try:
            body = browser.response_body(response.get("requestId"))
            entry["size"] = len(body)
            entry["body"] = body[:BODY_LIMIT]
            try:
                data = json.loads(body)
                entry["products_found"] = sum(1 for _ in dreampets.walk_products(data))
            except ValueError:
                pass
        except (cdp.BrowserError, TimeoutError) as error:
            entry["error"] = str(error)
        out.append(entry)
    return out


def make_report(base="https://dreampets.gg/mm2/", ours=None, folder=None, product_pages=5, log=None):
    """Собрать отчёт. ours — {адрес товара: что показывает программа}. -> путь к файлу."""
    log = log or (lambda text: None)
    report = {"time": datetime.now().isoformat(timespec="seconds"), "base": base, "cards": [], "card_html": [],
              "api": [], "pages": [], "notes": [], "ours": {}}
    taken = set()
    started = time.time()
    try:
        with cdp.Browser() as browser:
            browser.capture_network()
            log("открываю рынок DreamPets")
            browser.open(base, settle=3)
            result = browser.collect_cards('a[href*="/mm2/product/"]', timeout=90, max_pages=2)
            report["title"] = result.get("title")
            report["url"] = result.get("url")
            cards = result.get("cards") or []
            log(f"карточек: {len(cards)}")
            for card in cards[:CARDS_MAX]:
                href = card.get("href") or ""
                slug = re.search(r"/product/([^/]+)/", href)
                report["cards"].append({
                    "href": href, "text": (card.get("text") or "")[:800], "inner": (card.get("inner") or "")[:800],
                    "parsed": dreampets.parse_card_text(card.get("text"), slug.group(1).replace("-", " ") if slug else None),
                })
            try:
                report["card_html"] = browser.evaluate(JS_CARD_HTML, timeout=20) or []
            except (cdp.BrowserError, TimeoutError) as error:
                report["notes"].append(f"разметка карточек: {error}")
            report["api"] += _responses(browser, taken)
            for card in cards[:product_pages]:
                url = card.get("href")
                if not url:
                    continue
                log(f"страница товара: {url}")
                page = {"url": url}
                try:
                    browser.open(url, settle=3)
                    page["text"] = (browser.evaluate("document.body ? document.body.innerText : ''", timeout=20) or "")[:15000]
                    html = browser.evaluate("document.documentElement.outerHTML", timeout=30) or ""
                    page["html_size"] = len(html)
                    page["head"] = html[:15000]
                    page["parsed"] = dreampets.parse_product_page(html, url)
                    page["embedded_json"] = len(list(dreampets.embedded_json(html)))
                except (cdp.BrowserError, TimeoutError) as error:
                    page["error"] = str(error)
                report["pages"].append(page)
                report["api"] += _responses(browser, taken)
            report["notes"] += browser.notes
    except (cdp.BrowserError, TimeoutError, OSError) as error:
        report["notes"].append(f"браузер: {error}")
    for entry in report["cards"] + report["pages"]:
        url = entry.get("href") or entry.get("url")
        key = dreampets.uuid_of(url or "") or url
        for known, shown in (ours or {}).items():
            if key and key in str(known):
                report["ours"][url] = shown
    report["seconds"] = round(time.time() - started, 1)
    folder = Path(folder or Path(store.DATA_FILE).parent / "debug" / "dreampets")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"dreampets-check-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return path, report
