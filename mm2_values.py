"""Цены оружия и предметов MM2 по сайту Supreme Values (https://supremevalues.com/mm2).

Запуск (или MM2Values.exe с теми же командами):
    python mm2_values.py                 открыть программу в браузере
    python mm2_values.py update          обновить цены с сайта
    python mm2_values.py find chroma     найти предмет в консоли
    python mm2_values.py import page.txt --category godlies
                                         загрузить текст, скопированный со страницы сайта
"""

import argparse
import json
import sys
import threading
import urllib.request
import webbrowser

from supreme import markets, parser, paths, rates, server, store


def running_instance(port, tries=20):
    """Адрес уже запущенной программы (повторный двойной щелчок по exe).

    Проверяются те же порты, которые программа занимает, если основной занят.
    """
    for candidate in range(port, port + tries):
        url = f"http://127.0.0.1:{candidate}/"
        try:
            with urllib.request.urlopen(url + "api/status", timeout=0.5) as response:
                status = json.loads(response.read().decode("utf-8"))
        except Exception:
            continue
        if isinstance(status, dict) and "running" in status and "finished_at" in status:
            return url
    return None


def cmd_run(args):
    existing = running_instance(args.port)
    if existing:
        print(f"Программа уже запущена: {existing}")
        if not args.no_browser:
            webbrowser.open(existing)
        return
    # Перенос цен прошлой версии — только когда точно не запущена другая копия.
    store.use_data_dir(paths.migrate_old_data())
    monitor = None if args.no_markets else markets.MarketMonitor()
    rate = rates.RateWatcher()
    httpd = server.make_server(
        port=args.port, auto_sync=not (args.no_update or args.no_auto), monitor=monitor, rate=rate
    )
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    if not args.no_update and not store.DATA_FILE.exists():
        httpd.job.start()  # первый запуск: сразу скачать цены
    if httpd.sync:
        httpd.sync.start()  # следить за сайтом и подтягивать новые цены
    if monitor:
        monitor.start()  # опрашивать торговые площадки
    if not args.no_markets:
        rate.start()  # курс доллара с Rapira
    print(f"Программа открыта: {url}")
    print(f"Цены хранятся в: {store.DATA_FILE}")
    print("Чтобы закрыть программу, закройте это окно или нажмите Ctrl+C.")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def program_command():
    return "MM2Values.exe" if paths.FROZEN else "python mm2_values.py"


def cmd_update(args):
    store.use_data_dir(paths.migrate_old_data())
    try:
        store.update_from_site()
    except store.UpdateError as error:
        print(error, file=sys.stderr)
        return 1
    except OSError as error:
        print(f"Не удалось сохранить цены: {error}", file=sys.stderr)
        return 1
    return 0


def cmd_find(args):
    try:
        data = store.load()
    except (OSError, ValueError) as error:
        print(f"Файл с ценами повреждён ({store.DATA_FILE}): {error}", file=sys.stderr)
        print(f"Обновите цены: {program_command()} update", file=sys.stderr)
        return 1
    query = " ".join(args.query).lower()
    found = [item for item in data["items"] if query in item["name"].lower()]  # типы проверены в store.load
    if not data["items"]:
        print(f"Цен пока нет. Запустите: {program_command()} update")
        return 1
    if not found:
        print("Ничего не найдено.")
        return 1
    found.sort(key=lambda item: (item["value"] is None, -(item["value"] or 0), item["name"]))
    for item in found:
        value = item["value"] if item["value"] is not None else item["value_text"] or "—"
        title = parser.CATEGORY_TITLES.get(item["category"], item["category"])
        print(f"{item['name']} [{title}]: {value}")
    print(f"\nЦены на сайте обновлены: {data.get('site_last_updated') or '—'}")
    return 0


def read_text_file(path):
    """Текст из файла в любой обычной кодировке Windows (UTF-8, UTF-16, cp1251)."""
    with open(path, "rb") as file:
        raw = file.read()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1251", "replace")


def cmd_import(args):
    store.use_data_dir(paths.migrate_old_data())
    try:
        text = read_text_file(args.file)
        items, last_updated = store.import_text(text, args.category)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    except OSError as error:
        print(f"Не удалось прочитать или сохранить файл: {error}", file=sys.stderr)
        return 1
    print(f"Загружено предметов: {len(items)}")
    if last_updated:
        print(f"Дата обновления цен на сайте: {last_updated}")
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:  # вывод в файл или другую программу — UTF-8, чтобы не было "???"
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    arg_parser = argparse.ArgumentParser(description="Цены MM2 по сайту Supreme Values")
    arg_parser.add_argument("--port", type=int, default=8765, help="порт программы (по умолчанию 8765)")
    arg_parser.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    arg_parser.add_argument("--no-update", action="store_true", help="не скачивать цены при запуске и не следить за сайтом")
    arg_parser.add_argument("--no-auto", action="store_true", help="не проверять сайт автоматически")
    arg_parser.add_argument("--no-markets", action="store_true", help="не опрашивать торговые площадки")
    commands = arg_parser.add_subparsers(dest="command")
    commands.add_parser("update", help="обновить цены с сайта")
    find = commands.add_parser("find", help="найти предмет")
    find.add_argument("query", nargs="+")
    importer = commands.add_parser("import", help="загрузить скопированный текст страницы")
    importer.add_argument("file")
    importer.add_argument("--category", required=True, choices=[slug for slug, _ in parser.CATEGORIES])

    args = arg_parser.parse_args(argv)
    handlers = {None: cmd_run, "update": cmd_update, "find": cmd_find, "import": cmd_import}
    return handlers[args.command](args) or 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as error:  # в exe окно закрылось бы сразу и ошибку не было бы видно
        if not paths.FROZEN:
            raise
        print(f"Ошибка: {error}", file=sys.stderr)
        code = 1
    if code and paths.FROZEN and sys.stdin and sys.stdin.isatty():
        input("Нажмите Enter, чтобы закрыть окно…")
    sys.exit(code)
