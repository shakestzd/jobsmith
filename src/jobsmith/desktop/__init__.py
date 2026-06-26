"""jobsmith.desktop — Tauri desktop integration (feat-b621a4ab).

Houses the PyInstaller sidecar entry point that runs the existing FastAPI
application as a self-contained binary launched and managed by the Tauri shell.
This package is purely additive: the ``jobsmith`` CLI and the standalone
FastAPI server behave identically whether or not this package is present.
"""

from __future__ import annotations
