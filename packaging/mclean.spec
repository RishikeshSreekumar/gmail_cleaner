# PyInstaller spec for the standalone `mclean` binary.
#
# Textual ships .tcss stylesheets and keyring resolves its backends by entry
# point, so both need collecting explicitly -- a plain module scan misses them.
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

# On macOS, signing has to happen here rather than after the fact: a onefile
# build unpacks its own dylibs at runtime, and Apple wants every one of them
# signed, not just the executable wrapped around them. Given an identity,
# PyInstaller signs each collected binary and then the executable, with the
# hardened runtime and a timestamp -- which is what notarization checks for.
# Without one it ad-hoc signs, exactly as it did before.
codesign_identity = os.environ.get("MACOS_CODESIGN_IDENTITY") or None
entitlements_file = "entitlements.plist" if codesign_identity else None

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
    codesign_identity=codesign_identity,
    entitlements_file=entitlements_file,
)
