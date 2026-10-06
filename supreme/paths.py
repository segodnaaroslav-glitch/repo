"""Пути программы: и при запуске из исходников, и внутри MM2Values.exe."""

import os
import shutil
import sys
import tempfile
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
_SOURCE_DIR = Path(__file__).resolve().parent.parent


def resource_dir():
    """Папка с файлами интерфейса (в exe — временная папка распаковки)."""
    return Path(getattr(sys, "_MEIPASS", _SOURCE_DIR))


def app_dir():
    """Папка, где лежит программа (exe или mm2_values.py)."""
    return Path(sys.executable).resolve().parent if FROZEN else _SOURCE_DIR


def _writable(folder):
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=str(folder)):
            pass
    except OSError:
        return False
    return True


def _user_data_dir():
    if os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "MM2Values" / "data"
    return Path.home() / ".mm2values" / "data"


# Файлы программы в папке с ценами (переносятся из папки data прошлых версий).
# values.json переносится последним: его появление в новой папке значит, что перенос закончен.
_OWNED_FILES = ("markets-state.json", "markets.json", "rate.json")
_OWNED_DIRS = ("debug",)


def _old_data_dir():
    return app_dir() / "data"


def _migration_pending(old, target):
    return (old / "values.json").is_file() and not (target / "values.json").exists()


def data_dir():
    """Папка с ценами (без побочных действий: ничего не создаёт и не переносит).

    В MM2Values.exe — системная папка пользователя (%LOCALAPPDATA%\\MM2Values\\data),
    чтобы рядом с exe не появлялось никаких папок; пока цены прошлой версии
    (data рядом с exe) не перенесены — та папка. Из исходников — data рядом
    с mm2_values.py.
    """
    if FROZEN:
        target = _user_data_dir()
        old = _old_data_dir()
        return old if _migration_pending(old, target) else target
    preferred = app_dir() / "data"
    if _writable(preferred):
        return preferred
    return _user_data_dir()


def _copy_file(src, dst):
    part = dst.with_name(dst.name + ".part")
    shutil.copy2(str(src), str(part))
    os.replace(str(part), str(dst))


def migrate_old_data():
    """Перенести цены прошлой версии из папки data рядом с exe в папку пользователя.

    Переносятся только файлы программы (чужие файлы и сама папка остаются на месте).
    Сначала всё копируется, и только потом старые файлы удаляются; если копирование
    не удалось, программа работает со старой папкой и повторит перенос при следующем
    запуске. Возвращает папку, с которой работать.
    """
    if not FROZEN:
        return data_dir()
    target = _user_data_dir()
    old = _old_data_dir()
    if not _migration_pending(old, target):
        return data_dir()
    names = [name for name in _OWNED_FILES if (old / name).is_file()]
    names += sorted(path.name for path in old.glob("values.broken-*.json") if path.is_file())
    try:
        target.mkdir(parents=True, exist_ok=True)
        for name in names:
            _copy_file(old / name, target / name)
        for name in _OWNED_DIRS:
            if (old / name).is_dir():
                try:  # отладочные страницы не важны — их ошибка перенос не останавливает
                    shutil.copytree(str(old / name), str(target / name), dirs_exist_ok=True)
                except (OSError, shutil.Error):
                    pass
        _copy_file(old / "values.json", target / "values.json")
    except OSError:
        return old
    for name in names + ["values.json"]:
        try:
            (old / name).unlink()
        except OSError:
            pass
    for name in _OWNED_DIRS:
        shutil.rmtree(str(old / name), ignore_errors=True)
    try:
        old.rmdir()  # только если в ней больше ничего нет
    except OSError:
        pass
    return target
