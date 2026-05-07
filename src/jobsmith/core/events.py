"""``PipelineEvent`` — phase-granular events emitted by the apply pipeline.

Single canonical location for the event dataclass. ``jobsmith.apply``
re-exports this name for back-compat; new code should import from
``jobsmith.core``.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PipelineEvent:
    """Phase-granular event emitted by the apply pipeline generator.

    Attributes
    ----------
    kind:
        Event kind. One of:

        - ``"phase_started"``  — phase loop entered.
        - ``"phase_complete"`` — phase emitted ``<<PHASE_COMPLETE>>``.
        - ``"phase_failed"``   — phase emitted ``<<PHASE_FAILED>>``.
        - ``"slug_changed"``   — canonical slug differs from starting slug
          after gather-phase reconciliation.
        - ``"guard_failed"``   — anchor-guard step returned non-zero.
        - ``"cancelled"``      — pipeline stopped because the cancel
          token was set.

    phase:
        Phase name at time of event (``"gather"``, ``"draft"``, ``"render"``).

    payload:
        Kind-specific dict. ``"slug_changed"`` carries
        ``{"old_slug": ..., "new_slug": ...}``; ``"guard_failed"`` carries
        ``{"rc": ...}``; ``"phase_failed"`` carries ``{"reason": ...}``;
        others are ``{}``.
    """

    kind: str
    phase: str
    payload: dict = field(default_factory=dict)
