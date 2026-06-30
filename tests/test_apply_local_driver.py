"""Tests for the code-orchestrated LOCAL apply driver (feat-d67d2b1a, slice 1).

These exercise the orchestration SPINE only — no specialist logic, no network.
Backends are injected stubs implementing the slice-2 contract:

    complete_structured(messages, schema, *, temperature) -> (dict | None, bool)

Acceptance (done_when) proven here:
  1. Pipeline runs sequential / fan-out (concurrent, joined) / loop stages;
     a status=halt node short-circuits and surfaces its reason.
  2. A parse_ok=true checkpoint is skipped and its cached result returned
     (resume); a parse_ok=false checkpoint re-runs; a fresh run writes the
     checkpoint atomically.
  3. Node.run does a bounded reask (default 3) on invalid/unparseable JSON and
     returns (obj, parse_ok) — it NEVER raises on a single bad generation.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from pydantic import BaseModel

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
    SequentialStage,
)

SLUG = "acme-software-engineer"


# ---------------------------------------------------------------------------
# Output contracts for the stub nodes
# ---------------------------------------------------------------------------


class Value(BaseModel):
    value: int


class Decision(BaseModel):
    decision: str


def _schema(model: type[BaseModel], name: str) -> dict:
    return {"type": "json_schema", "json_schema": {"name": name, "schema": model.model_json_schema()}}


# ---------------------------------------------------------------------------
# Stub backends (the real Backend lands in slice 2)
# ---------------------------------------------------------------------------


class ValueBackend:
    """Always returns a valid Value payload. Records call count."""

    def __init__(self, value: int = 1) -> None:
        self.value = value
        self.calls = 0

    def complete_structured(self, messages, schema, *, temperature=0.0):
        self.calls += 1
        return {"value": self.value}, True


class DecisionBackend:
    """Returns a queued sequence of decisions; repeats the last when exhausted."""

    def __init__(self, decisions: list[str]) -> None:
        self.decisions = decisions
        self.calls = 0

    def complete_structured(self, messages, schema, *, temperature=0.0):
        idx = min(self.calls, len(self.decisions) - 1)
        self.calls += 1
        return {"decision": self.decisions[idx]}, True


class SequenceBackend:
    """Returns a fixed sequence of (dict|None, bool) tuples — for reask tests."""

    def __init__(self, responses: list[tuple[dict | None, bool]]) -> None:
        self.responses = responses
        self.calls = 0

    def complete_structured(self, messages, schema, *, temperature=0.0):
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return self.responses[idx]


class BadBackend:
    """Never produces a parseable object."""

    def __init__(self) -> None:
        self.calls = 0

    def complete_structured(self, messages, schema, *, temperature=0.0):
        self.calls += 1
        return None, False


class BarrierBackend:
    """Proves true concurrency: every call must reach the barrier within the
    timeout, otherwise the barrier breaks and ``concurrent_ok`` flips False."""

    def __init__(self, parties: int, timeout: float = 3.0) -> None:
        self.barrier = threading.Barrier(parties, timeout=timeout)
        self.concurrent_ok = True
        self.calls = 0
        self._lock = threading.Lock()

    def complete_structured(self, messages, schema, *, temperature=0.0):
        with self._lock:
            self.calls += 1
        try:
            self.barrier.wait()
        except threading.BrokenBarrierError:
            with self._lock:
                self.concurrent_ok = False
        return {"value": 1}, True


def _node(name: str, *, model=Value, halt_predicate=None, max_reask=3) -> Node:
    return Node(
        name,
        model,
        _schema(model, name),
        halt_predicate=halt_predicate,
        max_reask=max_reask,
    )


# ===========================================================================
# done_when 3 — bounded reask, never raises on a single bad generation
# ===========================================================================


def test_node_default_max_reask_is_three():
    assert _node("n").max_reask == 3


def test_node_reasks_bad_then_good_and_returns_parse_ok():
    backend = SequenceBackend(
        [
            (None, False),          # unparseable
            ({"wrong": 1}, True),   # parseable JSON but fails validation
            ({"value": 7}, True),   # finally valid
        ]
    )
    result = _node("jd-parse").run(backend)
    assert backend.calls == 3
    assert result.parse_ok is True
    assert result.status == "ok"
    assert result.data == {"value": 7}


def test_node_exhausts_reasks_without_raising():
    backend = BadBackend()
    result = _node("jd-parse", max_reask=3).run(backend)  # must NOT raise
    assert backend.calls == 3
    assert result.parse_ok is False
    assert result.status == "halt"
    assert result.data is None
    assert "jd-parse" in (result.reason or "")


def test_node_halt_predicate_surfaces_reason():
    backend = ValueBackend()
    node = _node("gap-check", halt_predicate=lambda obj: "no-fabrication: gap")
    result = node.run(backend)
    assert result.parse_ok is True
    assert result.status == "halt"
    assert result.reason == "no-fabrication: gap"


# ===========================================================================
# done_when 1 — sequential / fan-out / loop / halt short-circuit
# ===========================================================================


def test_pipeline_sequential_runs_all_nodes(tmp_path: Path):
    backend = ValueBackend(value=5)
    pipeline = Pipeline(
        [SequentialStage(_node("a")), SequentialStage(_node("b"))],
        slug=SLUG,
        root=tmp_path,
    )
    result = pipeline.run(backend)
    assert result.status == "ok"
    assert set(result.results) == {"a", "b"}
    assert result.results["a"].data == {"value": 5}
    assert backend.calls == 2


def test_pipeline_halt_short_circuits_and_surfaces(tmp_path: Path):
    backend = ValueBackend()
    pipeline = Pipeline(
        [
            SequentialStage(_node("a")),
            SequentialStage(_node("b", halt_predicate=lambda obj: "forced halt")),
            SequentialStage(_node("c")),
        ],
        slug=SLUG,
        root=tmp_path,
    )
    result = pipeline.run(backend)
    assert result.status == "halt"
    assert result.halt_node == "b"
    assert result.reason == "forced halt"
    assert "c" not in result.results  # short-circuited before node c


def test_pipeline_fanout_runs_concurrently_and_joins(tmp_path: Path):
    nodes = [_node("f1"), _node("f2"), _node("f3")]
    backend = BarrierBackend(parties=len(nodes))
    pipeline = Pipeline(
        [FanOutStage(nodes, max_workers=len(nodes))],
        slug=SLUG,
        root=tmp_path,
    )
    result = pipeline.run(backend)
    assert result.status == "ok"
    assert set(result.results) == {"f1", "f2", "f3"}  # joined: all present
    assert backend.calls == 3
    assert backend.concurrent_ok is True  # all three reached the barrier together


def test_pipeline_fanout_halt_short_circuits_following_stage(tmp_path: Path):
    fan = [
        _node("f1"),
        _node("f2", halt_predicate=lambda obj: "fanout halt"),
        _node("f3"),
    ]
    backend = ValueBackend()
    pipeline = Pipeline(
        [FanOutStage(fan, max_workers=3), SequentialStage(_node("after"))],
        slug=SLUG,
        root=tmp_path,
    )
    result = pipeline.run(backend)
    assert result.status == "halt"
    assert result.halt_node == "f2"
    assert "after" not in result.results
    # the fan-out still joined all three before short-circuiting
    assert {"f1", "f2", "f3"} <= set(result.results)


def test_loop_reruns_until_ok_predicate(tmp_path: Path):
    backend = DecisionBackend(["revise", "pass"])
    node = _node("qa", model=Decision)
    stage = LoopStage(node, ok=lambda r: r.data and r.data.get("decision") == "pass", max_iter=3)
    pipeline = Pipeline([stage], slug=SLUG, root=tmp_path)
    result = pipeline.run(backend)
    assert result.status == "ok"
    assert backend.calls == 2
    assert result.results["qa"].data == {"decision": "pass"}
    # a satisfied loop persists its final result
    assert read_checkpoint(SLUG, "qa", root=tmp_path) is not None


def test_loop_halts_on_max_iter_exhaustion(tmp_path: Path):
    backend = DecisionBackend(["revise"])  # never reaches "pass"
    node = _node("qa", model=Decision)
    stage = LoopStage(node, ok=lambda r: r.data.get("decision") == "pass", max_iter=2)
    pipeline = Pipeline(
        [stage, SequentialStage(_node("after"))],
        slug=SLUG,
        root=tmp_path,
    )
    result = pipeline.run(backend)
    assert result.status == "halt"
    assert result.halt_node == "qa"
    assert backend.calls == 2  # exactly max_iter attempts
    assert "after" not in result.results


# ===========================================================================
# done_when 2 — checkpoint resume / re-run / atomic write
# ===========================================================================


def test_checkpoint_path_threads_slug(tmp_path: Path):
    state = apply_state_dir(SLUG, root=tmp_path)
    expected = state / "jd-parse.json"
    assert checkpoint_path(SLUG, "jd-parse", root=tmp_path) == expected
    assert str(state).endswith(f"{SLUG}/.apply-state")  # slug threaded into the path


def test_apply_state_dir_defaults_to_private_applications(tmp_path: Path):
    # Default config.output.applications_dir is private/applications — NOT a bare
    # applications/ (roborev 1061: local artifacts must land where the app reads).
    assert apply_state_dir(SLUG, root=tmp_path) == tmp_path / "private" / "applications" / SLUG / ".apply-state"


def test_apply_state_dir_honors_configured_applications_dir(tmp_path: Path):
    import yaml

    (tmp_path / ".apply-config.yaml").write_text(
        yaml.safe_dump({"output": {"applications_dir": "custom/apps"}}), encoding="utf-8"
    )
    assert apply_state_dir(SLUG, root=tmp_path) == tmp_path / "custom" / "apps" / SLUG / ".apply-state"


def test_atomic_write_leaves_no_tmp_and_is_readable(tmp_path: Path):
    result = NodeResult(name="jd-parse", status="ok", parse_ok=True, data={"value": 1})
    path = write_checkpoint(SLUG, "jd-parse", result.model_dump(), root=tmp_path)
    assert path.exists()
    on_disk = json.loads(path.read_text())
    assert on_disk["parse_ok"] is True
    assert on_disk["data"] == {"value": 1}
    # tmp+rename leaves no scratch files behind
    assert list(path.parent.glob("*.tmp")) == []


def test_resume_skips_node_with_parse_ok_true_checkpoint(tmp_path: Path):
    write_checkpoint(
        SLUG,
        "a",
        {"name": "a", "status": "ok", "parse_ok": True, "data": {"value": 99}},
        root=tmp_path,
    )
    backend = ValueBackend(value=1)
    pipeline = Pipeline([SequentialStage(_node("a"))], slug=SLUG, root=tmp_path)
    result = pipeline.run(backend)
    assert backend.calls == 0  # skipped — cached result returned
    assert result.results["a"].data == {"value": 99}


def test_parse_ok_false_checkpoint_reruns_and_overwrites(tmp_path: Path):
    write_checkpoint(
        SLUG,
        "a",
        {"name": "a", "status": "halt", "parse_ok": False, "data": None},
        root=tmp_path,
    )
    assert read_checkpoint(SLUG, "a", root=tmp_path) is None  # treated as absent
    backend = ValueBackend(value=42)
    pipeline = Pipeline([SequentialStage(_node("a"))], slug=SLUG, root=tmp_path)
    result = pipeline.run(backend)
    assert backend.calls == 1  # re-ran
    assert result.results["a"].data == {"value": 42}
    on_disk = read_checkpoint(SLUG, "a", root=tmp_path)
    assert on_disk is not None and on_disk["parse_ok"] is True


def test_fresh_run_writes_checkpoint(tmp_path: Path):
    backend = ValueBackend(value=3)
    pipeline = Pipeline([SequentialStage(_node("a"))], slug=SLUG, root=tmp_path)
    pipeline.run(backend)
    cached = read_checkpoint(SLUG, "a", root=tmp_path)
    assert cached is not None
    assert cached["data"] == {"value": 3}


def test_halt_result_is_not_checkpointed(tmp_path: Path):
    backend = ValueBackend()
    pipeline = Pipeline(
        [SequentialStage(_node("a", halt_predicate=lambda obj: "gap"))],
        slug=SLUG,
        root=tmp_path,
    )
    pipeline.run(backend)
    # halts must re-evaluate on resume — never cached as final
    assert read_checkpoint(SLUG, "a", root=tmp_path) is None
    assert not checkpoint_path(SLUG, "a", root=tmp_path).exists()
