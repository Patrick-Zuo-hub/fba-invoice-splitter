# PyInstaller one-folder definition for the Windows portable build.
#
# One-folder, not one-file: a single-file build unpacks tens of megabytes to a
# temporary directory on every start, which both slows launch and moves the
# runtime directory somewhere the single-instance check cannot reason about.
#
# Build with scripts/build_windows.ps1 rather than invoking this directly, so
# the working directory and output paths stay consistent.

from pathlib import Path

PROJECT_ROOT = Path(SPECPATH).resolve().parent
SOURCE_ROOT = PROJECT_ROOT / "src"

# These three resolve through __file__ or importlib.resources at runtime, so
# the bundled layout has to mirror the source layout exactly.
datas = [
    (str(SOURCE_ROOT / "fis" / "web" / "templates"), "fis/web/templates"),
    (str(SOURCE_ROOT / "fis" / "web" / "static"), "fis/web/static"),
    (str(SOURCE_ROOT / "fis" / "xlsx" / "assets"), "fis/xlsx/assets"),
]

analysis = Analysis(
    [str(SOURCE_ROOT / "fis" / "__main__.py")],
    pathex=[str(SOURCE_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        # Uvicorn resolves its protocol implementations by name at runtime, so
        # static analysis does not see them.
        "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets.wsproto_impl",
        "uvicorn.lifespan.off",
        "uvicorn.lifespan.on",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "playwright", "mypy", "ruff"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="FIS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

collect = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="FIS",
)
