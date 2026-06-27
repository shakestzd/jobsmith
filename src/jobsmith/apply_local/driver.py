"""Driver core for the code-orchestrated LOCAL apply path (feat-d67d2b1a).

This is the orchestration SPINE — pure stdlib + pydantic, no framework
(LangGraph / Burr / Haystack were rejected for a fixed DAG). Two primitives:

* :class:`Node` — ONE bounded LLM task. Python owns the control flow; the model
  only does the single structured-JSON call. ``Node.run`` reasks up to a cap on
  invalid/unparseable output and NEVER raises on a single bad generation — it
  returns a :class:`NodeResult` with ``parse_ok=False`` instead.

* :class:`Pipeline` — executes an ordered list of stages over a ``slug``. Stage
  kinds: :class:`SequentialStage`, :class:`FanOutStage` (concurrent via a small
  ``ThreadPoolExecutor``, joined), and :class:`LoopStage` (re-run until an
  ok-predicate or a max-iteration cap). A node returning ``status=halt``
  short-circuits the pipeline and surfaces the reason — this is the v1
  no-fabrication policy: never fabricate to fill a gap.

The backend is INJECTED (see :class:`StructuredBackend`); the real one lands in
slice 2. Concurrency uses a SMALL worker cap because one local engine serializes
generation — real parallelism only matters once hybrid cloud routing exists.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, ValidationError

from jobsmith.apply_local.checkpoint import read_checkpoint, write_checkpoint

Status = Literal["ok", "halt"]

DEFAULT_MAX_REASK = 3
DEFAULT_FANOUT_WORKERS = 4  # small: one local engine serializes generation
_ERR_PREVIEW = 200


@runtime_checkable
class StructuredBackend(Protocol):
    """The injected model client. The real implementation lands in slice 2.

    Returns ``(payload, parse_ok)``: a JSON object dict (or ``None``) and a flag
    for whether a JSON object was successfully parsed from the generation.
    """

    def complete_structured(
        self,
        messages: list[dict],
        schema: dict,
        *,
        temperature: float = 0.0,
    ) -> tuple[dict | None, bool]: ...


# Builds the chat messages for a node from the run context.
MessageBuilder = Callable[[Mapping[str, Any]], list[dict]]
# Inspects a validated result and returns a halt reason, or None to proceed.
HaltPredicate = Callable[[BaseModel], str | None]
# Decides whether a loop result is "good enough" to stop iterating.
OkPredicate = Callable[["NodeResult"], bool]


class NodeResult(BaseModel):
    """The single return value of :meth:`Node.run` and the checkpoint envelope.

    Bundles the three things a node produces: the validated payload (``data``,
    or ``None`` when parsing ultimately failed), ``parse_ok``, and a coarse
    ``status`` (``ok`` | ``halt``). ``reason`` carries the halt explanation.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    status: Status = "ok"
    parse_ok: bool = False
    reason: str | None = None
    data: dict | None = None

    @property
    def is_final(self) -> bool:
        """True iff this is a cacheable success (parsed AND not halted)."""
        return self.parse_ok and self.status == "ok"


def _short(err: object) -> str:
    text = str(err).replace("\n", " ").strip()
    return text[:_ERR_PREVIEW]


class Node:
    """One bounded structured-JSON task. The model does it; Python decides when.

    Parameters
    ----------
    name:
        Checkpoint key (``applications/{slug}/.apply-state/<name>.json``).
    model_cls:
        Pydantic model the generation is validated against.
    json_schema:
        OpenAI-style ``response_format`` dict passed verbatim to the backend.
    build_messages:
        Optional ``context -> messages`` builder. Defaults to a system+user
        prompt embedding the schema and ``context["prompt"]``.
    max_reask:
        Bounded reask cap on invalid/unparseable output (default 3).
    halt_predicate:
        Optional ``validated_obj -> reason | None``. A returned reason makes the
        node halt (no-fabrication gate) even though parsing succeeded.
    temperature:
        Sampling temperature forwarded to the backend.
    """

    def __init__(
        self,
        name: str,
        model_cls: type[BaseModel],
        json_schema: dict,
        *,
        build_messages: MessageBuilder | None = None,
        max_reask: int = DEFAULT_MAX_REASK,
        halt_predicate: HaltPredicate | None = None,
        temperature: float = 0.0,
    ) -> None:
        if not name:
            raise ValueError("Node requires a non-empty name.")
        self.name = name
        self.model_cls = model_cls
        self.json_schema = json_schema
        self.max_reask = max(1, max_reask)
        self.halt_predicate = halt_predicate
        self.temperature = temperature
        self._build_messages = build_messages or self._default_build_messages

    def _default_build_messages(self, context: Mapping[str, Any]) -> list[dict]:
        inner = self.json_schema.get("json_schema", {})
        schema_obj = inner.get("schema", self.json_schema)
        prompt = str(context.get("prompt", "")) if isinstance(context, Mapping) else ""
        return [
            {
                "role": "system",
                "content": (
                    "You extract structured data. Respond with ONE JSON object "
                    "matching this schema and NOTHING else:\n"
                    f"{json.dumps(schema_obj)}"
                ),
            },
            {"role": "user", "content": prompt},
        ]

    def _attempt(
        self, backend: StructuredBackend, messages: list[dict]
    ) -> tuple[BaseModel | None, str]:
        """Run one generation. Returns ``(validated_obj_or_None, error)``.

        Backend transport errors are caught and reported as a failed attempt so
        a single bad call triggers a reask rather than crashing the pipeline.
        """
        try:
            payload, parsed = backend.complete_structured(
                messages, self.json_schema, temperature=self.temperature
            )
        except Exception as exc:  # noqa: BLE001 — any backend error is a reask, never a crash
            return None, f"backend error: {_short(exc)}"
        if not parsed or payload is None:
            return None, "backend returned no parseable JSON object"
        try:
            return self.model_cls.model_validate(payload), ""
        except ValidationError as exc:
            return None, _short(exc)

    def _reask(self, error: str) -> list[dict]:
        return [
            {
                "role": "user",
                "content": (
                    f"Previous response was invalid ({error}). Re-emit ONLY a single "
                    "JSON object matching the schema, with no extra text."
                ),
            }
        ]

    def run(self, backend: StructuredBackend, *, context: Mapping[str, Any] | None = None) -> NodeResult:
        """Run the bounded reask loop and return a :class:`NodeResult`.

        Never raises on a single bad/unparseable generation: it reasks up to
        ``max_reask`` times, then returns ``parse_ok=False``/``status=halt``.
        """
        messages = list(self._build_messages(context or {}))
        last_error = "no attempts made"
        for _ in range(self.max_reask):
            obj, error = self._attempt(backend, messages)
            if obj is not None:
                reason = self.halt_predicate(obj) if self.halt_predicate else None
                return NodeResult(
                    name=self.name,
                    status="halt" if reason else "ok",
                    parse_ok=True,
                    reason=reason,
                    data=obj.model_dump(mode="json"),
                )
            last_error = error
            messages = messages + self._reask(error)
        return NodeResult(
            name=self.name,
            status="halt",
            parse_ok=False,
            reason=f"{self.name}: invalid structured output after {self.max_reask} attempts: {last_error}",
            data=None,
        )


def _from_cache(name: str, cached: Mapping[str, Any]) -> NodeResult:
    """Reconstruct a :class:`NodeResult` from a cached envelope."""
    return NodeResult(
        name=name,
        status=cached.get("status", "ok"),
        parse_ok=bool(cached.get("parse_ok", True)),
        reason=cached.get("reason"),
        data=cached.get("data"),
    )


def _persist_if_final(slug: str, result: NodeResult, root: Any) -> None:
    """Checkpoint a result only when it is a cacheable success."""
    if result.is_final:
        write_checkpoint(slug, result.name, result.model_dump(mode="json"), root=root)


def run_node(
    node: Node,
    backend: StructuredBackend,
    *,
    slug: str,
    root: Any = None,
    ctx: Mapping[str, Any] | None = None,
) -> NodeResult:
    """Resume-aware node execution: skip on a cached success, else run + persist."""
    cached = read_checkpoint(slug, node.name, root=root)
    if cached is not None:
        return _from_cache(node.name, cached)
    result = node.run(backend, context=ctx)
    _persist_if_final(slug, result, root)
    return result


@runtime_checkable
class Stage(Protocol):
    """A pipeline stage: runs one or more nodes and returns their results."""

    def run(
        self,
        backend: StructuredBackend,
        *,
        slug: str,
        root: Any,
        ctx: Mapping[str, Any],
    ) -> list[NodeResult]: ...


class SequentialStage:
    """Run a single node."""

    def __init__(self, node: Node) -> None:
        self.node = node

    def run(self, backend, *, slug, root, ctx) -> list[NodeResult]:
        return [run_node(self.node, backend, slug=slug, root=root, ctx=ctx)]


class FanOutStage:
    """Run N nodes concurrently via a small ``ThreadPoolExecutor`` and join.

    Results are returned in the nodes' declared order for determinism. The
    worker count is capped (``DEFAULT_FANOUT_WORKERS``) because one local engine
    serializes generation; a higher cap only helps hybrid cloud routing.
    """

    def __init__(self, nodes: Sequence[Node], *, max_workers: int = DEFAULT_FANOUT_WORKERS) -> None:
        self.nodes = list(nodes)
        if not self.nodes:
            raise ValueError("FanOutStage requires at least one node.")
        self.max_workers = max(1, max_workers)

    def run(self, backend, *, slug, root, ctx) -> list[NodeResult]:
        workers = min(self.max_workers, len(self.nodes))
        order = {node.name: i for i, node in enumerate(self.nodes)}
        results: list[NodeResult] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(run_node, node, backend, slug=slug, root=root, ctx=ctx)
                for node in self.nodes
            ]
            for future in as_completed(futures):
                results.append(future.result())
        results.sort(key=lambda r: order[r.name])
        return results


class LoopStage:
    """Re-run a node until ``ok(result)`` is true or ``max_iter`` is reached.

    A ``halt`` during iteration short-circuits immediately. Exhausting
    ``max_iter`` without satisfying ``ok`` surfaces a halt when
    ``halt_on_exhaust`` (default True) — mirroring the apply-prose-qa contract
    (iteration == max && unresolved → halt). The cache is consulted once on
    entry (resume), but never mid-loop, so the loop actually iterates.
    """

    def __init__(
        self,
        node: Node,
        ok: OkPredicate,
        *,
        max_iter: int = DEFAULT_MAX_REASK,
        halt_on_exhaust: bool = True,
    ) -> None:
        self.node = node
        self.ok = ok
        self.max_iter = max(1, max_iter)
        self.halt_on_exhaust = halt_on_exhaust

    def run(self, backend, *, slug, root, ctx) -> list[NodeResult]:
        cached = read_checkpoint(slug, self.node.name, root=root)
        if cached is not None:
            return [_from_cache(self.node.name, cached)]
        result: NodeResult | None = None
        for _ in range(self.max_iter):
            result = self.node.run(backend, context=ctx)
            if result.status == "halt":
                return [result]
            if self.ok(result):
                _persist_if_final(slug, result, root)
                return [result]
        assert result is not None  # max_iter >= 1 guarantees one iteration
        if self.halt_on_exhaust:
            result = result.model_copy(
                update={
                    "status": "halt",
                    "reason": result.reason
                    or f"loop '{self.node.name}' exhausted {self.max_iter} iterations "
                    "without satisfying its ok-predicate",
                }
            )
        return [result]


class PipelineResult(BaseModel):
    """The outcome of a full :meth:`Pipeline.run`."""

    model_config = ConfigDict(extra="ignore")

    status: Status
    results: dict[str, NodeResult] = {}
    halt_node: str | None = None
    reason: str | None = None

    def data(self, name: str) -> dict | None:
        node = self.results.get(name)
        return node.data if node else None


class Pipeline:
    """Execute an ordered list of stages over one application ``slug``.

    Each stage's results are folded into a shared context (``ctx[name] = data``)
    so downstream nodes can read upstream outputs. The first stage that yields a
    ``halt`` result short-circuits the pipeline and surfaces the reason.
    """

    def __init__(self, stages: Sequence[Stage], *, slug: str, root: Any = None) -> None:
        if not slug:
            raise ValueError("Pipeline requires a non-empty slug.")
        self.stages = list(stages)
        self.slug = slug
        self.root = root

    def run(
        self, backend: StructuredBackend, *, context: Mapping[str, Any] | None = None
    ) -> PipelineResult:
        ctx: dict[str, Any] = dict(context or {})
        collected: dict[str, NodeResult] = {}
        for stage in self.stages:
            results = stage.run(backend, slug=self.slug, root=self.root, ctx=ctx)
            for result in results:
                collected[result.name] = result
                ctx[result.name] = result.data
            halted = next((r for r in results if r.status == "halt"), None)
            if halted is not None:
                return PipelineResult(
                    status="halt",
                    results=collected,
                    halt_node=halted.name,
                    reason=halted.reason,
                )
        return PipelineResult(status="ok", results=collected)


__all__ = [
    "Status",
    "StructuredBackend",
    "NodeResult",
    "Node",
    "run_node",
    "Stage",
    "SequentialStage",
    "FanOutStage",
    "LoopStage",
    "PipelineResult",
    "Pipeline",
    "DEFAULT_MAX_REASK",
    "DEFAULT_FANOUT_WORKERS",
]
