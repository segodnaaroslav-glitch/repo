"""Цены оружия и предметов MM2 по сайту Supreme Values (https://supremevalues.com/mm2).

Запуск:
    python mm2_values.py                 открыть программу в браузере
    python mm2_values.py update          обновить цены с сайта
    python mm2_values.py find chroma     найти предмет в консоли
    python mm2_values.py import page.txt --category godlies
                                         загрузить текст, скопированный со страницы сайта
"""

import argparse
import sys
import threading
import webbrowser

from supreme import parser, server, store


def cmd_run(args):
    httpd = server.make_server(port=args.port)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    if not store.DATA_FILE.exists():
        httpd.job.start()  # первый запуск: сразу скачать цены
    print(f"Программа открыта: {url}")
    print("Чтобы закрыть программу, закройте это окно или нажмите Ctrl+C.")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def cmd_update(args):
    try:
        store.update_from_site()
    except store.UpdateError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


def cmd_find(args):
    data = store.load()
    query = " ".join(args.query).lower()
    found = [item for item in data["items"] if query in item["name"].lower()]
    if not data["items"]:
        print("Цен пока нет. Запустите: python mm2_values.py update")
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


def cmd_import(args):
    with open(args.file, encoding="utf-8") as file:
        text = file.read()
    try:
        items = store.import_text(text, args.category)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"Загружено предметов: {len(items)}")
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass

    arg_parser = argparse.ArgumentParser(description="Цены MM2 по сайту Supreme Values")
    arg_parser.add_argument("--port", type=int, default=8765, help="порт программы (по умолчанию 8765)")
    arg_parser.add_argument("--no-browser", action="store_true", help="не открывать браузер")
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
    sys.exit(main())
