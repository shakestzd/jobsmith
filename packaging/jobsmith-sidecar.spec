# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the jobsmith desktop sidecar (feat-b621a4ab, slice 1).

Builds a self-contained ONEFILE binary from
``src/jobsmith/desktop/sidecar_main.py``.  The FastAPI app factory is referenced
by string in ``uvicorn.run("jobsmith.api.main:create_app", ...)``, so PyInstaller
cannot discover the jobsmith API import graph statically — every jobsmith
submodule is collected explicitly via ``collect_submodules``.  Heavy third-party
packages with data files / dynamic imports (uvicorn, fastapi, playwright,
claude_agent_sdk) are pulled in with ``collect_all``.

The built web UI (``src/jobsmith/web_dist/``) is bundled as data when present so
the binary serves the SPA; ``find_web_dist()`` resolves it relative to the
``jobsmith`` package inside the unpacked onefile bundle.

Build via ``scripts/build-sidecar.sh`` (which stages web_dist first and renames
the output into ``src-tauri/binaries/jobsmith-sidecar-<triple>``).
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

# SPECPATH is injected by PyInstaller as the directory holding this spec file.
PROJECT_ROOT = Path(SPECPATH).parent  # noqa: F821  (SPECPATH provided by PyInstaller)
SRC_DIR = PROJECT_ROOT / "src"
ENTRY = SRC_DIR / "jobsmith" / "desktop" / "sidecar_main.py"

datas: list = []
binaries: list = []
hiddenimports: list = []

# Third-party packages whose data files / dynamic imports PyInstaller misses.
for _pkg in ("uvicorn", "fastapi", "playwright", "claude_agent_sdk"):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

# The app factory is referenced by string, so collect the whole jobsmith tree.
hiddenimports += collect_submodules("jobsmith")

# Bundle the built UI if it has been pre-staged. find_web_dist() looks for
# <package_root>/web_dist, i.e. ``jobsmith/web_dist`` inside the bundle.
_web_dist = SRC_DIR / "jobsmith" / "web_dist"
if _web_dist.is_dir():
    datas.append((str(_web_dist), "jobsmith/web_dist"))

a = Analysis(  # noqa: F821
    [str(ENTRY)],
    pathex=[str(SRC_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="jobsmith-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
