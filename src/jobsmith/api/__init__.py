"""jobsmith HTTP API package.

Public surface:
    create_app — FastAPI application factory.
"""

from __future__ import annotations

from jobsmith.api.main import create_app

__all__ = ["create_app"]
