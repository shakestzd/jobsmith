"""End-to-end code-orchestrated LOCAL apply (feat-70b1b976, slice 7).

Wires the gather (slice 5) + draft (slice 6) pipelines with PER-NODE backends
(slice 2) over a managed vllm-mlx engine (slice 4):

* resolve ``config.llm.apply`` and build the gather + draft pipelines;
* resolve a backend per node and dispatch each structured call to it
  (``RoutingBackend``) so a hybrid run mixes local gemma + cloud Claude;
* ensure the local engine is healthy when any node is local, surfacing the
  ~20-30s cold load;
* on a non-ok pipeline, distinguish a no-fabrication HALT / model parse failure
  from an ENGINE-DOWN crash via :func:`vllm_mlx.health`, and report the
  ``on_failure`` policy (error | fallback_cloud) to the caller.

The cloud ``claude -p`` path is untouched — ``_cli_apply.run_apply`` only routes
here when ``orchestrator == "code_local"``; the fallback-to-cloud decision is
enacted by that caller (this module never re-enters it, so no recursion).
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from jobsmith.apply_local.backends import OpenAICompatBackend, resolve_backend
from jobsmith.apply_local.checkpoint import apply_state_dir
from jobsmith.apply_local.driver import Pipeline, PipelineResult, StructuredBackend
from jobsmith.apply_local.nodes_draft import ART_PROSE_DRAFT, build_draft_pipeline
from jobsmith.apply_local.nodes_gather import build_gather_pipeline
from jobsmith.apply_local.render import render_local
from jobsmith.apply_local.run_record import finalize_run, open_run_record
from jobsmith.apply_local.schemas import (
    ART_BULLET_SELECTION,
    ART_FIT_SCORE,
    ART_JD_PARSED,
)
from jobsmith.config import JobsmithConfig
from jobsmith.llm import vllm_mlx

logger = logging.getLogger(__name__)

ENGINE_READY_TIMEOUT_S = 180.0
ENGINE_POLL_INTERVAL_S = 2.0

# Outcome statuses
OK = "ok"
HALT = "halt"
ENGINE_DOWN = "engine_down"

# Draft-context keys -> the on-disk gather artifact each is loaded from.
_GATHER_ARTIFACTS = {
    "jd_parsed": ART_JD_PARSED,
    "fit_score": ART_FIT_SCORE,
    "bullet_selection": ART_BULLET_SELECTION,
}


class EngineUnavailableError(RuntimeError):
    """The local engine could not be made healthy (not installed / crashed / timeout)."""


@dataclass
class ApplyOutcome:
    """Result of a ``code_local`` apply run."""

    status: str  # OK | HALT | ENGINE_DOWN
    slug: str
    reason: str | None = None
    halt_node: str | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    fallback_cloud: bool = False  # caller should route to claude -p
    # Local render status ("ok" | "skipped" | "error" | None). A "skipped" (no
    # quarto) or "error" never demotes ``status`` from OK — the tailored
    # gather/draft artifacts are valuable and kept — but it is surfaced here (and
    # in ``reason`` for an error) so the CLI / run-history can report it.
    render_status: str | None = None
    # False when the deterministic QA checks flag the resume's actual work.yml
    # bullets (roborev 1066) — surfaced so unchecked bullet text is never silent.
    resume_qa_pass: bool = True

    @property
    def ok(self) -> bool:
        return self.status == OK


# ---------------------------------------------------------------------------
# Per-node routing backend
# ---------------------------------------------------------------------------


class RoutingBackend:
    """A :class:`StructuredBackend` that dispatches each call to the backend
    resolved for the node whose ``json_schema`` name matches the incoming schema.

    This is how per-node routing is achieved without mutating the driver: the
    ``Node`` passes its own ``json_schema`` (carrying a stable ``name``) to
    ``complete_structured``, and we look the backend up by that name.
    """

    def __init__(
        self, by_schema_name: dict[str, StructuredBackend], *, default: StructuredBackend | None = None
    ) -> None:
        self._by_name = dict(by_schema_name)
        self._default = default
        self.routed: list[str] = []

    def complete_structured(self, messages: list[dict], schema: dict, *, temperature: float = 0.0):
        name = _schema_name_of(schema) or ""
        self.routed.append(name)
        backend = self._by_name.get(name, self._default)
        if backend is None:
            raise RuntimeError(f"no backend resolved for schema {name!r}")
        return backend.complete_structured(messages, schema, temperature=temperature)


def _schema_name_of(schema: Any) -> str | None:
    if not isinstance(schema, dict):
        return None
    inner = schema.get("json_schema")
    if isinstance(inner, dict) and inner.get("name"):
        return inner["name"]
    return schema.get("name")


def _backend_nodes(pipeline: Pipeline) -> Iterable[Any]:
    """Yield the backend-calling ``Node`` of each stage.

    Unwraps ``ProseDraftNode`` (the draft loop node) to its inner prose-write
    ``Node`` — that inner node actually calls the backend and carries the
    ``json_schema`` used for routing.
    """
    for stage in pipeline.stages:
        nodes = getattr(stage, "nodes", None)
        if nodes is None:
            single = getattr(stage, "node", None)
            nodes = [single] if single is not None else []
        for node in nodes:
            inner = getattr(node, "write_node", None)
            yield inner if inner is not None else node


def build_routing_backend(
    config: JobsmithConfig, pipelines: Iterable[Pipeline], *, local_base_url: str | None = None
) -> RoutingBackend:
    """Resolve a backend per node and key it by the node's json_schema name.

    When ``local_base_url`` is given, every resolved local (OpenAI-compatible)
    backend is pointed at the managed engine's actual address.
    """
    by_name: dict[str, StructuredBackend] = {}
    cache: dict[str, StructuredBackend] = {}
    for pipeline in pipelines:
        for node in _backend_nodes(pipeline):
            schema_name = _schema_name_of(getattr(node, "json_schema", None))
            node_name = getattr(node, "name", None)
            if not schema_name or not node_name or schema_name in by_name:
                continue
            backend = cache.get(node_name)
            if backend is None:
                backend = resolve_backend(config, node_name)
                if local_base_url and isinstance(backend, OpenAICompatBackend):
                    backend.base_url = local_base_url
                cache[node_name] = backend
            by_name[schema_name] = backend
    return RoutingBackend(by_name)


# ---------------------------------------------------------------------------
# Engine lifecycle
# ---------------------------------------------------------------------------


def _engine_model(config: JobsmithConfig) -> str:
    node_backend = config.llm.apply.node_backend
    if node_backend is not None and node_backend.model:
        return node_backend.model
    return vllm_mlx._DEFAULT_MODEL


def _configured_endpoint(config: JobsmithConfig) -> str | None:
    node_backend = config.llm.apply.node_backend
    if node_backend is not None and node_backend.provider == "openai_compatible":
        return node_backend.base_url
    return None


def _port_of(base_url: str | None) -> int:
    # base_url like http://127.0.0.1:59609/v1
    if not base_url:
        return 0
    try:
        return int(base_url.split(":")[2].split("/")[0])
    except (IndexError, ValueError):
        return 0


def _endpoint_live(base_url: str | None) -> bool:
    return bool(base_url) and vllm_mlx._models_ready(_port_of(base_url))


def ensure_engine(config: JobsmithConfig, *, model: str | None = None) -> tuple[str, bool]:
    """Return ``(base_url, managed)`` for a healthy local engine.

    Reuses a user-run endpoint already serving at the configured base_url
    (``managed=False``); otherwise starts and waits on our own managed engine
    serving ``model`` (``managed=True``). ``model`` should be the model the
    managed local nodes actually target (roborev 1065); falls back to the
    default ``node_backend`` model. Raises :class:`EngineUnavailableError` if it
    cannot be made ready.
    """
    configured = _configured_endpoint(config)
    if _endpoint_live(configured):
        logger.info("apply_local: using already-serving engine at %s", configured)
        return configured, False

    model = model or _engine_model(config)
    try:
        vllm_mlx.start(model)
    except vllm_mlx.VllmMlxNotInstalledError as exc:
        raise EngineUnavailableError(str(exc)) from exc

    deadline = time.monotonic() + ENGINE_READY_TIMEOUT_S
    while time.monotonic() < deadline:
        health = vllm_mlx.health()
        state = health.get("state")
        if state == vllm_mlx.STATE_READY:
            return health["base_url"], True
        if state == vllm_mlx.STATE_CRASHED:
            raise EngineUnavailableError("vllm-mlx engine crashed during load")
        time.sleep(ENGINE_POLL_INTERVAL_S)
    raise EngineUnavailableError(f"engine not ready within {ENGINE_READY_TIMEOUT_S:.0f}s")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _discard_prior_run(slug: str, repo_root: Any) -> None:
    """Remove any prior ``.apply-state`` + ``documents`` for a forced fresh run.

    Without this, checkpoint resume (and an already-built resume.qmd) would make
    ``--force`` return stale outputs (roborev 1065).
    """
    import shutil

    state = apply_state_dir(slug, root=repo_root)
    for d in (state, state.parent / "documents"):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)


def _load_draft_context(slug: str, repo_root: Any) -> dict[str, Any]:
    """Read the gather artifacts back into the draft pipeline's context keys."""
    state = apply_state_dir(slug, root=repo_root)
    ctx: dict[str, Any] = {}
    for key, filename in _GATHER_ARTIFACTS.items():
        path = state / filename
        ctx[key] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return ctx


def _artifact_paths(slug: str, repo_root: Any) -> dict[str, str]:
    state = apply_state_dir(slug, root=repo_root)
    names = [*(_GATHER_ARTIFACTS.values()), ART_PROSE_DRAFT]
    return {name: str(state / name) for name in names if (state / name).exists()}


def _classify_failure(
    result: PipelineResult, *, slug: str, engine_managed: bool, on_failure: str
) -> ApplyOutcome:
    """Turn a non-ok pipeline result into an outcome, distinguishing an
    ENGINE-DOWN crash (-> on_failure policy) from a genuine HALT / parse failure.
    """
    if engine_managed and vllm_mlx.health().get("state") == vllm_mlx.STATE_CRASHED:
        return ApplyOutcome(
            status=ENGINE_DOWN,
            slug=slug,
            reason=f"local engine crashed (last stage: {result.halt_node or result.status})",
            fallback_cloud=(on_failure == "fallback_cloud"),
        )
    return ApplyOutcome(status=HALT, slug=slug, reason=result.reason, halt_node=result.halt_node)


def _finish_render(
    outcome: ApplyOutcome, *, slug: str, config: JobsmithConfig, repo_root: Any
) -> None:
    """Render the resume + assemble the portfolio, folding results into outcome.

    Best-effort: :func:`render_local` never raises. A ``"skipped"`` (no quarto)
    leaves the apply OK with no fake PDF; an ``"error"`` is surfaced in
    ``outcome.reason`` while the gather/draft artifacts in ``outcome.artifacts``
    are preserved.
    """
    render = render_local(slug, config, repo_root=repo_root)
    outcome.render_status = render.status
    outcome.resume_qa_pass = render.qa_pass
    outcome.artifacts.update(render.artifacts)
    if render.pdf_path:
        outcome.artifacts["resume_pdf"] = render.pdf_path
    if render.status == "error":
        outcome.reason = render.reason
    elif not render.qa_pass:
        cats = "; ".join(sorted({str(f.get("category", "?")) for f in render.qa_findings}))
        outcome.reason = f"resume work bullets have unresolved QA findings: {cats}"


def run_local_apply(
    *,
    jd_text: str,
    slug: str,
    config: JobsmithConfig,
    repo_root: Any = None,
    jd_url: str | None = None,
    backend: StructuredBackend | None = None,
    run_id: str | None = None,
    force: bool = False,
) -> ApplyOutcome:
    """Run a code_local gather->draft->render apply for ``slug`` on ``jd_text``.

    Builds the gather + draft pipelines, resolves a per-node backend (ensuring
    the local engine when any node is local), runs gather then draft, writes the
    ``applications/{slug}/.apply-state`` artifacts, then renders the resume PDF
    and assembles the portfolio PURELY in code (no ``claude -p``). The apply is
    recorded in the pipeline ``apply_runs`` table (a clean no-op when no config
    DB is present). ``run_id`` ties the record to the caller's run; ``force``
    discards any prior ``.apply-state``/``documents`` so a fresh run never
    resumes stale artifacts (roborev 1065). When ``backend`` is injected (tests /
    explicit backend), the engine is not managed.
    """
    on_failure = config.llm.apply.on_failure
    if force:
        _discard_prior_run(slug, repo_root)
    gather = build_gather_pipeline(config, slug, root=repo_root)
    draft = build_draft_pipeline(config, slug, root=repo_root)

    engine_managed = False
    stop_engine = False
    if backend is None:
        routing = build_routing_backend(config, [gather, draft])
        local = [b for b in routing._by_name.values() if isinstance(b, OpenAICompatBackend)]
        # Per-node: a backend already pointing at a LIVE endpoint (e.g. a specific
        # node_backends override, or a user-run engine) is used AS-IS. Only nodes
        # whose endpoint is not live need the managed engine — and only those get
        # their base_url set to it, so explicit per-node endpoints are never
        # clobbered (roborev 1061).
        need_managed = [b for b in local if not _endpoint_live(b.base_url)]
        if need_managed:
            # Serve the model the managed nodes actually target — not merely the
            # default node_backend model (roborev 1065).
            managed_model = next((b.model for b in need_managed if getattr(b, "model", None)), None)
            try:
                base_url, managed = ensure_engine(config, model=managed_model)
            except EngineUnavailableError as exc:
                return ApplyOutcome(
                    status=ENGINE_DOWN,
                    slug=slug,
                    reason=str(exc),
                    fallback_cloud=(on_failure == "fallback_cloud"),
                )
            engine_managed = True  # a local engine is in play (ours or a user's)
            stop_engine = managed  # only tear down an engine we started
            for b in need_managed:
                b.base_url = base_url
        elif local:
            engine_managed = True  # all local nodes use live endpoints; still health-classifiable
        backend = routing

    db_conn, db_run_id = open_run_record(repo_root, slug=slug, run_id=run_id)
    db_final_status = "failed"
    try:
        gres = gather.run(backend, context={"jd_text": jd_text, "jd_url": jd_url})
        if gres.status != "ok":
            return _classify_failure(gres, slug=slug, engine_managed=engine_managed, on_failure=on_failure)

        dres = draft.run(backend, context=_load_draft_context(slug, repo_root))
        if dres.status != "ok":
            return _classify_failure(dres, slug=slug, engine_managed=engine_managed, on_failure=on_failure)

        outcome = ApplyOutcome(status=OK, slug=slug, artifacts=_artifact_paths(slug, repo_root))
        _finish_render(outcome, slug=slug, config=config, repo_root=repo_root)
        # A render "error" finalizes the run as failed (mirroring the cloud
        # late-render-failure convention) while keeping the early artifacts; a
        # clean render or a quarto-absent "skipped" finalizes as done.
        db_final_status = "failed" if outcome.render_status == "error" else "done"
        return outcome
    finally:
        finalize_run(db_conn, db_run_id, slug, db_final_status)
        if stop_engine:
            vllm_mlx.stop()


__all__ = [
    "ApplyOutcome",
    "EngineUnavailableError",
    "RoutingBackend",
    "build_routing_backend",
    "ensure_engine",
    "run_local_apply",
    "OK",
    "HALT",
    "ENGINE_DOWN",
]
