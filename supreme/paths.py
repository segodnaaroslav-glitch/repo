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


def data_dir():
    """Папка с ценами.

    В MM2Values.exe — системная папка пользователя (%LOCALAPPDATA%\\MM2Values\\data),
    чтобы рядом с exe не появлялось никаких папок. Папка data от прошлых версий
    (рядом с exe) переносится туда. Из исходников — data рядом с mm2_values.py.
    """
    if FROZEN:
        target = _user_data_dir()
        old = app_dir() / "data"
        if old.is_dir() and not target.exists():
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old), str(target))
            except OSError:
                return old  # не получилось перенести — работаем со старой папкой
        if _writable(target):
            return target
    preferred = app_dir() / "data"
    if _writable(preferred):
        return preferred
    if os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "MM2Values" / "data"
    return Path.home() / ".mm2values" / "data"
