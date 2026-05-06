"""Reference :class:`~jobsmith.core.protocols.ConfirmGate` implementations.

- :class:`AutoYesGate` always proceeds — used by the FastAPI app where
  there is no terminal user, and by ``--yes`` CLI runs.
- :class:`ClickConfirmGate` wraps ``click.confirm`` so terminal users get
  the historical inter-phase prompt.
"""
from __future__ import annotations

import click


class AutoYesGate:
    """Always advances. Use when no human is present at the gate.

    The supervisor uses this for every UI-launched run: the gate
    decision is made at the HTTP request level, not the subprocess
    level, so the pipeline must auto-proceed once the request lands.
    """

    def proceed(self, *, phase_name: str, phase_num: int) -> bool:  # noqa: D401
        """Return ``True`` unconditionally."""
        return True


class ClickConfirmGate:
    """Reads ``y/N`` from stdin via ``click.confirm``.

    Default ``False`` so a quiet stdin (e.g. ``</dev/null``) declines the
    gate cleanly rather than aborting. CLI users wanting auto-proceed
    pass ``--yes`` which selects :class:`AutoYesGate` instead — this gate
    only ever fires on interactive terminal runs without the flag.
    """

    def proceed(self, *, phase_name: str, phase_num: int) -> bool:  # noqa: D401
        """Prompt the operator. Return ``True`` to advance, ``False`` to stop."""
        prompt = f"Phase {phase_num} ({phase_name}) complete. Proceed to next phase?"
        try:
            return bool(click.confirm(prompt, default=False))
        except (click.Abort, EOFError):
            return False
