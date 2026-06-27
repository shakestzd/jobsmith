"""Code-orchestrated LOCAL apply path (feat-d67d2b1a, slice 1).

Python owns the apply DAG; the model does ONE bounded structured-JSON task per
node. The spike (docs/spikes/byo-model-apply.md) proved a 4B local model cannot
drive Claude Code's agentic loop, but does single bounded JSON calls reliably —
so the control flow lives here, not in the model.

This package is the orchestration SPINE only — no specialist logic:

* :mod:`jobsmith.apply_local.driver` — :class:`Node` (one bounded LLM task with
  a reask loop) and :class:`Pipeline` (sequential / fan-out / loop stages with a
  ``halt`` short-circuit).
* :mod:`jobsmith.apply_local.checkpoint` — atomic per-node checkpoints under
  ``applications/{slug}/.apply-state/<name>.json`` for crash-resume.

The backend (the thing that actually calls the model) is INJECTED — see the
``StructuredBackend`` protocol. The real backend lands in slice 2.
"""
from __future__ import annotations

from jobsmith.apply_local.backends import resolve_backend
from jobsmith.apply_local.checkpoint import (
    apply_state_dir,
    checkpoint_path,
    read_checkpoint,
    write_checkpoint,
)
from jobsmith.apply_local.driver import (
    FanOutStage,
    LoopStage,
    Node,
    NodeResult,
    Pipeline,
    PipelineResult,
    SequentialStage,
    Stage,
    StructuredBackend,
    run_node,
)

__all__ = [
    "Node",
    "NodeResult",
    "Pipeline",
    "PipelineResult",
    "Stage",
    "SequentialStage",
    "FanOutStage",
    "LoopStage",
    "StructuredBackend",
    "resolve_backend",
    "run_node",
    "apply_state_dir",
    "checkpoint_path",
    "read_checkpoint",
    "write_checkpoint",
]
