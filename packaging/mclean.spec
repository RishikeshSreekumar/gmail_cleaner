# PyInstaller spec for the standalone `mclean` binary.
#
# Textual ships .tcss stylesheets and keyring resolves its backends by entry
# point, so both need collecting explicitly -- a plain module scan misses them.
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [("../mailcleaner/static", "mailcleaner/static")]
binaries = []
hiddenimports = collect_submodules("keyring.backends")

for package in ("textual", "rich", "typer", "keyring"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

a = Analysis(
    ["entry.py"],
    pathex=[".."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="mclean",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)
