# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for asciify-ps.

Build with:  python -m PyInstaller --clean --noconfirm asciify-ps.spec
Output:      dist/asciify-ps.exe  (single file, no console window)
"""

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []

# tkinterdnd2 ships the tkdnd Tcl extension as package data.
for package in ("tkinterdnd2",):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# The vendored rendering library is imported through a sys.path insert, which
# static analysis cannot follow, so it is requested explicitly.
hiddenimports += [
    "asciify",
    "asciify.core",
    "asciify.process",
    "asciify.renderer",
    "asciify.utils",
]

datas += [("assets/icon.ico", "assets")]

a = Analysis(
    ["asciify_ps.py"],
    pathex=["asciify-them/src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "pytest",
        "IPython",
        "notebook",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "wx",
        "pandas",
        "scipy",
        "setuptools",
        "pip",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="asciify-ps",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/icon.ico",
)
