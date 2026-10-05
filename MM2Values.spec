# Сборка MM2Values.exe: pyinstaller --noconfirm MM2Values.spec
# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ["mm2_values.py"],
    datas=[("web", "web")],
    excludes=["tkinter", "unittest", "pydoc", "playwright"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="MM2Values",
    console=True,
    upx=False,
)
