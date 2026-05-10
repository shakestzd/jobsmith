from pathlib import Path

from fastapi import Request


def get_repo_root(request: Request) -> Path:
    """Return the repo root cached in app.state at lifespan startup."""
    return request.app.state.repo_root
