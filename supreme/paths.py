"""Пути программы: и при запуске из исходников, и внутри MM2Values.exe."""

import os
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


def data_dir():
    """Папка с ценами: data рядом с программой, а если туда нельзя писать —
    %LOCALAPPDATA%\\MM2Values\\data (или ~/.mm2values/data)."""
    preferred = app_dir() / "data"
    if _writable(preferred):
        return preferred
    if os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "MM2Values" / "data"
    return Path.home() / ".mm2values" / "data"
