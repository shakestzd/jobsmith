"""Uvicorn entry-point for the jobsmith HTTP API.

Called by the ``jobsmith api serve`` Typer subcommand.
"""

from __future__ import annotations

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def resolve_host(*, bind_public: bool) -> str:
    """Return the bind host based on the ``--bind-public`` flag.

    Parameters
    ----------
    bind_public:
        When True, bind to 0.0.0.0 (all interfaces).
        When False (default), bind to 127.0.0.1 (loopback only).
    """
    return "0.0.0.0" if bind_public else DEFAULT_HOST  # noqa: S104


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
