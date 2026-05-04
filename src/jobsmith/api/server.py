"""Uvicorn entry-point for the jobsmith HTTP API.

Called by the ``jobsmith api serve`` Typer subcommand.
"""

from __future__ import annotations


def serve(host: str, port: int, reload: bool) -> None:
    """Start the uvicorn server using the create_app factory."""
    import uvicorn

    uvicorn.run(
        "jobsmith.api.main:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
    )
