"""Marimo UI helpers for the chat sidebar (slice 5).

This module owns marimo rendering only — no logic, no DB access.
Import it inside marimo notebook cells to avoid dependency issues in tests.

Ported from moplan/plugin/notebooks/plan_ui.py::render_chat_history_bubbles.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import marimo as mo  # noqa: F401 — type-checker only


def render_chat_history_bubbles(history: list[dict], mo: Any) -> list:
    """Render chat messages as styled bubbles.

    User messages appear in blue (right-aligned).
    Assistant messages appear as neutral callouts.

    Parameters
    ----------
    history:
        List of dicts with keys ``role`` (``"user"``/``"assistant"``) and
        ``content`` (message text).
    mo:
        The ``marimo`` module (passed in by the notebook cell so this module
        stays importable without marimo installed in the test environment).

    Returns
    -------
    list
        List of ``mo.Html`` / ``mo.callout`` elements ready for ``mo.vstack``.
    """
    bubbles = []
    for msg in history:
        role = msg.get("role", "user")
        text = msg.get("content", "")
        # Unescape double-encoded strings (\\n → \n, \\" → ")
        if isinstance(text, str):
            text = text.replace("\\n", "\n").replace('\\"', '"')

        if role == "user":
            escaped = text.replace("<", "&lt;").replace(">", "&gt;")
            bubbles.append(
                mo.Html(
                    f'<div style="margin:6px 0;padding:8px 12px;background:#3b82f6;'
                    f"color:#fff;border-radius:12px 12px 4px 12px;font-size:13px;"
                    f'line-height:1.4;margin-left:20%">{escaped}</div>'
                )
            )
        else:
            bubbles.append(mo.callout(mo.md(text), kind="neutral"))
    return bubbles


__all__ = ["render_chat_history_bubbles"]
