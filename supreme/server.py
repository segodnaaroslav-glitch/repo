"""Веб-сервер программы: http://127.0.0.1:<порт>/ (и с другого компьютера — по паролю, если
доступ включён: см. remote.py)."""

import json
import os
import threading
import urllib.parse
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import base64
import binascii

from . import autosync, calc, dpreport, imgproxy, liquidity, markets, ocr, parser, paths, remote, store, tileocr

WEB_DIR = paths.resource_dir() / "web"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/picmatch.js": ("picmatch.js", "text/javascript; charset=utf-8"),
    "/pictiles.js": ("pictiles.js", "text/javascript; charset=utf-8"),
}
MAX_BODY = 5 * 1024 * 1024
MAX_CALC_BODY = 64 * 1024 * 1024  # несколько скриншотов (интерфейс уменьшает большие)
DRAIN_LIMIT = 256 * 1024 * 1024  # слишком большой запрос дочитывается, чтобы браузер увидел ответ
MAX_IMAGES = 8
MAX_BADGE_TILES = 300  # плиток на скриншот для повторного чтения значков «xN»
LARGE_POSTS = ("/api/calc", "/api/calc/badges")  # со скриншотами — больший предел размера


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
    images = None  # imgproxy.ImageCache — картинки предметов для сравнения по картинке
    features = None  # imgproxy.FeatureStore — их признаки (data/picfeat.json)
    pic_sources = None  # {"stamp", "sources", "keys", "lock"} — адреса картинок известных предметов
    remote_on = False  # программа слушает сеть: с других компьютеров — по паролю
    data_dir = None  # папка с ценами (там и настройка доступа с другого компьютера)
    login_guard = None  # remote.LoginGuard — защита от подбора пароля

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

    def _loopback(self):
        """Запрос с этого же компьютера (а не по сети)."""
        address = str(self.client_address[0] if self.client_address else "")
        return address.startswith("127.") or address in ("::1", "::ffff:127.0.0.1")

    def _remote_settings(self):
        return remote.load(self.data_dir or store.DATA_DIR)

    def _session_ok(self):
        """Вход с другого компьютера: доступ включён в этом запуске и браузер вошёл по паролю."""
        if not self.remote_on:
            return False
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        if remote.COOKIE not in cookie:
            return False
        return remote.check_session(self._remote_settings(), cookie[remote.COOKIE].value)

    def _host_ok(self):
        # С этого компьютера — по адресу 127.0.0.1 / localhost (чужой сайт, подменивший адрес
        # своего домена на 127.0.0.1, не пройдёт). С другого компьютера — только после входа.
        if self._loopback() and self._local_address(self.headers.get("Host", "")):
            return True
        return self._session_ok()

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        if origin.lower().startswith("http://") and self._local_address(origin[len("http://"):].rstrip("/")):
            return True
        # Страница программы, открытая с другого компьютера: тот же адрес, что и у запроса. Браузер
        # с запретом на передачу адреса страницы пишет Origin: null — чужой сайт и так не пройдёт:
        # cookie входа (SameSite=Strict) на запросы с других сайтов браузер не отдаёт.
        if not self._session_ok():
            return False
        return origin.strip().lower() == "null" or self._same_host(origin)

    def _same_host(self, origin):
        """Origin указывает на тот же адрес, по которому открыта программа (без учёта схемы и «/»)."""
        site = origin.strip().lower().rstrip("/").split("://", 1)[-1]
        return bool(site) and site == self.headers.get("Host", "").strip().lower()

    def _foreign(self):
        """Ответ «чужой сайт» — с тем, что пришло: по нему видно, если адрес подменяет прокси."""
        return self._error(403, f"Запрос с чужого сайта запрещён (Origin: {self.headers.get('Origin')}, "
                                f"Host: {self.headers.get('Host')})")

    def _denied(self, path):
        """Нет доступа: с другого компьютера при включённом доступе — страница входа по паролю."""
        if self.remote_on and not self._loopback():
            if path in ("/", "/index.html"):
                return self._send(200, remote.LOGIN_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return self._error(401, "Нужно войти по паролю")
        return self._error(403, "Неверный адрес")

    def _login(self, body):
        """Вход с другого компьютера по паролю (с паузами после неудачных попыток)."""
        if not self.remote_on or self._loopback():
            return self._error(404, "Не найдено")
        # Origin здесь не проверяется: войти можно только паролем, подбор ограничен, а вход «за
        # пользователя» с чужого сайта ничего не даёт (cookie входа чужим сайтам не отдаётся).
        address = str(self.client_address[0])
        wait = self.login_guard.wait(address)
        if wait:
            return self._error(429, f"Слишком много неверных попыток — подождите {wait} с")
        settings = self._remote_settings()
        if not settings["enabled"] or not remote.check_password(settings, body.get("password")):
            self.login_guard.failed(address)
            return self._error(401, "Неверный пароль")
        self.login_guard.passed(address)
        self.send_response(200)
        body_bytes = json.dumps({"ok": True}).encode("utf-8")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie", f"{remote.COOKIE}={remote.make_session(settings)}; Path=/; HttpOnly; "
                                       f"SameSite=Strict; Max-Age={remote.SESSION_DAYS * 86400}")
        self.end_headers()
        self.wfile.write(body_bytes)
        return None

    def _remote_state(self):
        settings = self._remote_settings()
        port = self.server.server_address[1]
        local = self._loopback()
        return {"enabled": settings["enabled"], "active": bool(self.remote_on), "local": local,
                "password": remote.has_password(settings), "port": port,
                "links": remote.links(port) if local else []}

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
        has_picked = isinstance(body.get("picked"), list) and bool(body["picked"])
        pictures = body.get("pictures") is True  # браузер умеет искать плитки по картинке
        if len(images) > MAX_IMAGES:
            return self._error(400, f"Не больше {MAX_IMAGES} скриншотов за раз")
        decoded, ocr_errors = [], []
        for index, image in enumerate(images, 1):
            if not isinstance(image, str):
                continue
            try:
                decoded.append((index, base64.b64decode(image.split(",", 1)[-1], validate=False)))
            except (binascii.Error, ValueError):
                ocr_errors.append(f"скриншот {index}: не картинка")
        if not text.strip() and not decoded and not has_picked:
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
        # Скриншоты: сначала как инвентарь MM2 (плитки: название на полосе + «xN»),
        # иначе — как обычный текст (список, чат).
        matcher = tileocr.NameMatcher([i["name"] for i in items if not i.get("secret")]) if decoded else None
        picked = calc.picked_entries(body.get("picked"), items)
        by_name = calc.name_index(items) if decoded else {}
        shots, ocr_lines, unknown_tiles, shot_info = [], [], 0, []
        found, unmatched = calc.match_text(text, items) if text.strip() else ([], [])
        for index, raw in decoded:
            # Для браузера: что распознавание нашло на скриншоте (index — номер в списке images с 0).
            info = {"index": index - 1, "width": None, "height": None, "mode": "error", "text_items": 0, "tiles": []}
            shot_info.append(info)
            try:
                words, tiles = ocr.read_inventory(raw, matcher)
            except ocr.OcrError as error:
                ocr_errors.append(f"скриншот {index}: {error}")
                continue
            except Exception as error:  # одна картинка не должна ломать весь расчёт
                ocr_errors.append(f"скриншот {index}: не удалось распознать ({error})")
                continue
            info["width"], info["height"] = words.get("width"), words.get("height")
            for number, tile in enumerate(tiles):
                tile["id"] = f"{index - 1}:{number}"
            if any(tile["name"] for tile in tiles):
                shots.append(tiles)
                unknown_tiles += sum(1 for tile in tiles if not tile["name"])
                info["mode"] = "tiles"
            else:
                # Каждый скриншот — отдельно: «x2» в конце одного не относится к началу другого,
                # а браузер может убрать строки скриншота, если по картинке это инвентарь.
                lines = ocr.lines_from_words(words)
                ocr_lines.extend(lines)
                info["mode"] = "text"
                shot_found, shot_unmatched = calc.match_text("\n".join(lines), items) if lines else ([], [])
                found.extend(shot_found)
                unmatched.extend(shot_unmatched)
                info["text_items"] = len(shot_found)
                info["text_rows"] = [{"key": markets.item_id(entry["item"]), "qty": entry["qty"],
                                      "score": round(entry["score"], 2), "seen": entry["seen"]} for entry in shot_found]
            info["tiles"] = calc.describe_tiles(tiles, by_name)
        if not text.strip() and not shots and not ocr_lines and not picked and not (pictures and shot_info):
            return self._error(400, "; ".join(ocr_errors) or "На скриншотах не найдено ни одного предмета")
        tile_found, tile_unmatched = calc.match_tiles(shots, items)
        merged = {}
        for entry in found:  # текст и скриншоты, прочитанные как текст: один предмет — одна строка
            key = markets.item_id(entry["item"])
            if key in merged:
                merged[key]["qty"] += entry["qty"]
                merged[key]["score"] = min(merged[key]["score"], entry["score"])
                merged[key]["seen"] += entry["seen"]
            else:
                merged[key] = dict(entry, seen=list(entry["seen"]))
        for entry in tile_found:
            key = markets.item_id(entry["item"])
            if key in merged:
                merged[key]["qty"] += entry["qty"]
                merged[key]["seen"] += entry["seen"]
                for field in ("alternatives", "ambiguous", "qty_uncertain", "tiles", "tile_qty"):
                    merged[key].setdefault(field, entry[field])
            else:
                merged[key] = entry
        for item, qty, via in picked:  # узнанное браузером по картинке или выбранное пользователем
            key = markets.item_id(item)
            entry = merged.setdefault(key, {"item": item, "qty": 0, "score": 1.0, "seen": [], "via": via})
            entry["qty"] = min(9999, entry["qty"] + qty)
        result = []
        for entry in merged.values():
            item = entry["item"]
            row = self._calc_item(item)
            row.update({
                "qty": entry["qty"],
                "score": round(entry["score"], 2),
                "seen": entry["seen"],
                "ambiguous": bool(entry.get("ambiguous")),
                "qty_uncertain": bool(entry.get("qty_uncertain")),
                "alternatives": [dict(self._calc_item(other), key=markets.item_id(other))
                                 for other in entry.get("alternatives") or []],
                "key": markets.item_id(item),
                "tiles": entry.get("tiles") or [],
                "tile_qty": entry.get("tile_qty") or 0,
                "via": entry.get("via") or ("name" if entry.get("tiles") else "text"),
            })
            result.append(row)
        return self._json(200, {
            "items": result,
            "unmatched": (unmatched + tile_unmatched)[:50],
            "ocr_lines": len(ocr_lines),
            "tiles": sum(len(tiles) for tiles in shots),
            "unknown_tiles": unknown_tiles,
            "ocr_errors": ocr_errors,
            "shots": shot_info,
            "rate": self.rate.status() if self.rate else None,
            "fees": {m["id"]: m.get("fee", 0) for m in (self.monitor.configs() if self.monitor else [])},
        })

    def _calc_badges(self, body):
        """Значки «xN» плиток, которые браузер нашёл по картинке (распознавание их не нашло):
        {"shots": [{"index", "image", "boxes": [[x, y, w, h]]}]} -> количество каждой плитки.
        Нет значка — 1 шт.; значок есть, но не прочитан — "qty_uncertain"."""
        shots = body.get("shots") if isinstance(body.get("shots"), list) else []
        if len(shots) > MAX_IMAGES:
            return self._error(400, f"Не больше {MAX_IMAGES} скриншотов за раз")
        result, errors = [], []
        for number, shot in enumerate(shots, 1):
            if not isinstance(shot, dict) or not isinstance(shot.get("image"), str):
                continue
            # Ответ — по плитке на каждое место из запроса (в том же порядке); неверное место — 1 шт.
            # «name» — чтобы tileocr.badge_regions взял плитку: название здесь не нужно.
            tiles = []
            for box in (shot.get("boxes") if isinstance(shot.get("boxes"), list) else [])[:MAX_BADGE_TILES]:
                ok = (isinstance(box, list) and len(box) == 4
                      and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in box)
                      and 8 <= box[2] <= 10000 and 8 <= box[3] <= 10000 and abs(box[0]) <= 10 ** 5 and abs(box[1]) <= 10 ** 5)
                tiles.append({"name": "?" if ok else None, "box": [int(round(v)) for v in box] if ok else None,
                              "qty": 1, "qty_from_badge": False})
            index = shot.get("index") if isinstance(shot.get("index"), int) else number - 1
            read = False
            if any(tile["box"] for tile in tiles):
                try:
                    raw = base64.b64decode(shot["image"].split(",", 1)[-1], validate=False)
                    regions = tileocr.badge_regions(tiles)
                    again = ocr.recognize_regions(raw, [area for _, area in regions])
                    tileocr.apply_region_badges(tiles, regions, again.get("words"), again.get("areas"))
                    read = True
                except (binascii.Error, ValueError):
                    errors.append(f"скриншот {index + 1}: не картинка")
                except ocr.OcrError as error:
                    errors.append(f"скриншот {index + 1}: {error}")
                except Exception as error:  # без количества, но с найденными предметами
                    errors.append(f"скриншот {index + 1}: значки не прочитаны ({error})")
            result.append({"index": index, "read": read, "tiles": [
                {"qty": int(t["qty"]), "qty_from_badge": bool(t["qty_from_badge"]),
                 "qty_uncertain": bool(t.get("qty_uncertain"))} for t in tiles]})
        return self._json(200, {"shots": result, "ocr_errors": errors})

    # --- картинки предметов (сравнение плиток со скриншотов по картинке) ---

    def _pic_sources(self):
        """({ключ: {"site"|"market": адрес}}, ключи всех предметов) — адреса картинок известных
        предметов. Пересчитывается, только когда изменились цены или данные площадок."""
        holder = self.pic_sources
        version = self.monitor.version if self.monitor else 0
        try:
            stamp = (os.stat(self.data_path or store.DATA_FILE).st_mtime_ns, version)
        except OSError:
            stamp = (0, version)
        with holder["lock"]:
            if holder["stamp"] == stamp:
                return holder["sources"], holder["keys"]
        items = [i for i in store.load(self.data_path)["items"] if not parser.is_placeholder(i) and not i.get("secret")]
        # Картинка StarPets нужна и тем, у кого есть картинка с сайта (два эталона на предмет):
        # annotate берёт её с площадки, только когда своей картинки у предмета нет.
        probes = [dict(item, image=None) for item in items]
        if self.monitor is not None:
            try:
                self.monitor.annotate(probes)
            except Exception:
                pass
        sources = {}
        for item, probe in zip(items, probes):
            if item["category"] == "sets":
                continue  # набор не лежит в инвентаре плиткой, а его картинка — один из предметов
            entry = {}
            for src, url in (("site", item.get("image")), ("market", probe.get("image_market"))):
                if isinstance(url, str) and imgproxy.host_allowed(url):
                    entry[src] = url
            if entry:
                sources[markets.item_id(item)] = entry
        keys = {markets.item_id(item) for item in items}
        with holder["lock"]:
            holder.update(stamp=stamp, sources=sources, keys=keys)
        return sources, keys

    def _same_site(self):
        """Картинки и признаки — только для страниц самой программы (не для чужих сайтов)."""
        return self.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")

    def _item_image(self, query):
        params = urllib.parse.parse_qs(query)
        key = (params.get("key") or [""])[0]
        src = (params.get("src") or ["site"])[0]
        try:
            url = self._pic_sources()[0].get(key, {}).get(src)
        except (OSError, ValueError) as error:
            return self._error(500, f"Не удалось прочитать цены: {error}")
        if not url:
            return self._error(404, "У предмета нет такой картинки")
        try:
            body, content_type = self.images.get(url)
        except imgproxy.ImageError as error:
            # kind — почему: «нет связи» браузер считает только по настоящим обрывам связи с сайтом
            # (network), а не по одной плохой картинке; site — с каким сайтом.
            return self._json(error.status, {"error": str(error), "kind": error.kind, "site": imgproxy.site_of(url)})
        except Exception as error:  # ответить, а не оборвать связь: браузер примет обрыв за «нет сети»
            return self._json(502, {"error": f"Картинка не загрузилась: {error}", "kind": "final",
                                    "site": imgproxy.site_of(url)})
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Адрес запроса — ключ предмета, а не адрес картинки: из кэша браузера после смены картинки
        # пришла бы старая, и её признаки сохранились бы для новой. Повтор дёшев — картинка на диске.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        self.end_headers()
        self.wfile.write(body)

    def _picfeat(self):
        try:
            sources, _ = self._pic_sources()
        except (OSError, ValueError):
            sources = {}
        self._send(200, self.features.snapshot_json({"sources": sources}), "application/json; charset=utf-8")

    def _save_picfeat(self, body):
        try:
            sources, keys = self._pic_sources()
            saved = self.features.update(body, sources, keys, set(parser.CATEGORY_TITLES))
        except ValueError as error:
            return self._error(409, str(error))
        except OSError as error:
            return self._error(500, f"Не удалось сохранить признаки картинок: {error}")
        except (TypeError, AttributeError, KeyError) as error:  # странный запрос — ответ, а не обрыв связи
            return self._error(400, f"Неверные признаки картинок: {error}")
        return self._json(200, {"saved": saved})

    def _dreampets_check(self):
        """«Проверить DreamPets»: отчёт о том, что площадка на самом деле показывает и отдаёт."""
        ours, base = {}, "https://dreampets.gg/mm2/"
        if self.monitor is not None:
            for market in self.monitor.markets:
                if market.config.get("kind") != "dreampets":
                    continue
                base = market.config.get("url") or base
                for key, offer in dict(market.offers).items():
                    ours[f"{key} {offer.get('url')}"] = {
                        "item": key, "name": offer.get("name"), "price": offer.get("price"),
                        "stock": offer.get("stock"), "rarity": offer.get("rarity"), "url": offer.get("url")}
                for key, entry in dict((market.cache or {}).get("products") or {}).items():
                    ours.setdefault(f"cache {key} {entry.get('url')}", dict(entry, cache_key=key))
        try:
            path, report = dpreport.make_report(base, ours=ours)
        except Exception as error:  # отчёт — подсказка разработчику, программу он не роняет
            return self._error(500, f"Не удалось проверить DreamPets: {error}")
        if os.name == "nt":
            try:
                os.startfile(str(path.parent))  # открыть папку с отчётом
            except OSError:
                pass
        return self._json(200, {"path": str(path), "cards": len(report["cards"]), "api": len(report["api"]),
                                "pages": len(report["pages"]), "notes": report["notes"][:5]})

    @staticmethod
    def _calc_item(item):
        market = item.get("market") or {}
        return {
            "name": item["name"],
            "category": item["category"],
            "image": item.get("image") or item.get("image_market"),
            "value": item.get("value"),
            "prices": {
                market_id: {"price": info.get("price"), "currency": info.get("currency"),
                            "price_rub": info.get("price_rub"),
                            "price_rub_approx": bool(info.get("price_rub_approx")),
                            "price_from": bool(info.get("price_from")),
                            "fee": info.get("fee", 0), "stock": info.get("stock")}
                for market_id, info in market.items()
            },
        }

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
        path = self.path.split("?", 1)[0]
        if not self._host_ok():
            return self._denied(path)
        if path == "/api/remote":
            return self._json(200, self._remote_state())
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
        if path in ("/api/itemimg", "/api/picfeat"):
            if not self._same_site():
                return self._error(403, "Запрос с чужого сайта запрещён")
            if path == "/api/picfeat":
                return self._picfeat()
            return self._item_image(self.path.partition("?")[2])
        return self._error(404, "Не найдено")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/login" and self.remote_on and not self._loopback():
            pass  # вход по паролю — до проверки входа
        elif not self._host_ok():
            return self._denied(path)
        elif not self._origin_ok():
            return self._foreign()
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._error(415, "Нужен JSON")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._error(400, "Неверная длина запроса")
        limit = MAX_CALC_BODY if self.path.split("?", 1)[0] in LARGE_POSTS else MAX_BODY
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
        if path == "/api/calc/badges":
            return self._calc_badges(body)
        if path == "/api/picfeat":
            return self._save_picfeat(body)
        if path == "/api/markets/poll":
            if self.monitor is None:
                return self._error(409, "Площадки выключены")
            self.monitor.poll_now()
            return self._json(202, self._status())
        if path == "/api/login":
            return self._login(body)
        if path == "/api/logout":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", "2")
            self.send_header("Set-Cookie", f"{remote.COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
            self.end_headers()
            self.wfile.write(b"{}")
            return None
        if path == "/api/remote":
            if not self._loopback():
                return self._error(403, "Доступ с другого компьютера настраивается там, где запущена программа")
            password, enabled = body.get("password"), body.get("enabled")
            if (password is not None and not isinstance(password, str)) or (enabled is not None and not isinstance(enabled, bool)):
                return self._error(400, "Нужны поля password и/или enabled")
            folder = self.data_dir or store.DATA_DIR
            try:
                if password is not None:
                    remote.set_password(folder, password)
                if enabled is not None:
                    remote.set_enabled(folder, enabled)
            except ValueError as error:
                return self._error(400, str(error))
            except OSError as error:
                return self._error(500, f"Не удалось сохранить настройку: {error}")
            return self._json(200, self._remote_state())
        if path == "/api/markets/link":
            if self.monitor is None:
                return self._error(409, "Площадки выключены")
            market, item = body.get("market"), body.get("item")
            offer, url = body.get("offer"), body.get("url")
            if isinstance(item, str) and isinstance(url, str) and url.strip():
                # Ссылка на страницу товара: площадка — по ссылке (StarPets или DreamPets).
                try:
                    market = self.monitor.link_url(item, url)
                except ValueError as error:
                    return self._error(400, str(error))
                except KeyError:
                    return self._error(404, "Эта площадка выключена")
                return self._json(200, {"linked": True, "market": market})
            if not isinstance(market, str) or not isinstance(item, str) or not (offer is None or isinstance(offer, (str, int))):
                return self._error(400, "Нужны market, item и offer (или url)")
            try:
                self.monitor.link(market, item, None if offer is None else str(offer))
            except KeyError:
                return self._error(404, "Нет такой площадки")
            return self._json(200, {"linked": offer is not None, "market": market})
        if path == "/api/dreampets/check":
            return self._dreampets_check()
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
                check_site=None, monitor=None, rate=None, remote_on=False):
    """Создать сервер; если порт занят — взять следующий свободный.

    auto_sync=True — следить за сайтом и обновлять цены автоматически
    (поток запускается методом server.sync.start()). remote_on=True — пускать другие
    компьютеры по паролю (тогда host — "0.0.0.0", программа слушает сеть).
    """
    job = UpdateJob(update or (lambda log: store.update_from_site(log=log, path=data_path)))
    sync = None
    if auto_sync:
        sync = autosync.AutoSync(job, data_path, **({"check_site": check_site} if check_site else {}))
    data_dir = Path(data_path).parent if data_path else store.DATA_DIR
    handler = type(
        "BoundHandler", (Handler,),
        {"job": job, "sync": sync, "monitor": monitor, "rate": rate, "data_path": data_path,
         "images": imgproxy.ImageCache(data_dir / "images"), "features": imgproxy.FeatureStore(data_dir / "picfeat.json"),
         "pic_sources": {"stamp": None, "sources": {}, "keys": set(), "lock": threading.Lock()},
         "remote_on": bool(remote_on), "data_dir": data_dir, "login_guard": remote.LoginGuard()},
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
