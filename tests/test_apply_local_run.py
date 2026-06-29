"""Tests for the end-to-end code-orchestrated LOCAL apply (feat-70b1b976, slice 7).

These exercise run.py's wiring over the integrated slices: gather (5) -> draft (6)
with PER-NODE backends (2) and a managed vllm-mlx engine (4). No live model and
no real engine — stub backends keyed on json_schema name + monkeypatched engine.

done_when proven here:
  2. A mid-run engine crash is detected as ENGINE-DOWN via health() (not misread
     as a node parse failure / halt) and reported per on_failure (error |
     fallback_cloud).
  3. With orchestrator=claude_cloud (default) run_apply routes to the existing
     claude -p path unchanged; with code_local it routes to run_local_apply.

Plus: orchestration order (gather->draft), genuine per-node routing flowing
through Pipeline.run (the wiring slice 6 deferred), and the happy-path artifacts.
(done_when #1's real gemma gather->draft is the manual orchestrator gate.)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from jobsmith.apply_local import run as run_mod
from jobsmith.apply_local.backends import AnthropicBackend, OpenAICompatBackend
from jobsmith.apply_local.nodes_draft import ART_PROSE_DRAFT, NODE_PROSE_WRITE
from jobsmith.apply_local.run import (
    ENGINE_DOWN,
    HALT,
    OK,
    ApplyOutcome,
    EngineUnavailableError,
    RoutingBackend,
    build_routing_backend,
    ensure_engine,
    run_local_apply,
)
from jobsmith.apply_local.schemas import (
    ART_BULLET_SELECTION,
    ART_FIT_SCORE,
    ART_JD_PARSED,
)
from jobsmith.config import JobsmithConfig, NodeBackendConfig
from jobsmith.guard import parse_master_bullets
from jobsmith.llm import vllm_mlx

SLUG = "helios-data-engineer"

# ---------------------------------------------------------------------------
# Sample master + voice fixtures (Pat-Doe-style — NOT the real user's content)
# ---------------------------------------------------------------------------

_WORK = [
    {
        "title": "Senior Data Engineer",
        "location": "Helios Energy",
        "date": "2024 - Present",
        "description": "Remote",
        "details": [
            "Unlocked $250M in tax credits across 200K solar assets via geospatial Python",
            "Mentored two analysts to handle business requests independently",
        ],
    },
    {
        "title": "Data Analyst",
        "location": "Atlas Capital",
        "date": "2020 - 2022",
        "description": "Remote",
        "details": [
            {
                "bullet": "Built quarterly investor reporting pipelines, 5 days to 4 hours",
                "anchor": True,
                "anchor_reason": "Story-of-impact bullet",
                "tags": ["reporting"],
            },
            "Co-designed a PostgreSQL data dictionary adopted across three teams",
        ],
    },
]
_SKILL = [{"title": "Programming", "description": "Python, SQL", "details": ["Python", "SQL"]}]
_EDUCATION = [
    {"title": "Northeastern University", "location": "Boston, MA", "date": "2018 - 2020",
     "description": "M.S. Data Analytics Engineering", "details": ["Thesis: geospatial ML"]}
]
_AUTHOR = {"author": [{"name": {"first": "Pat", "last": "Doe"}, "email": "pat@example.com"}]}
_PROFILE = {"user": {"name": "Pat Doe"}, "years": {"total_quantitative": 6}}
_VOICE = "Explorer not marketer. Specific not sweeping."


def _setup(root: Path) -> JobsmithConfig:
    content = root / "assets" / "content"
    content.mkdir(parents=True, exist_ok=True)
    (content / "work.yml").write_text(yaml.safe_dump(_WORK), encoding="utf-8")
    (content / "skill.yml").write_text(yaml.safe_dump(_SKILL), encoding="utf-8")
    (content / "education.yml").write_text(yaml.safe_dump(_EDUCATION), encoding="utf-8")
    (content / "author.yml").write_text(yaml.safe_dump(_AUTHOR), encoding="utf-8")
    cap = root / "private" / "capacity"
    cap.mkdir(parents=True, exist_ok=True)
    (cap / "profile.yaml").write_text(yaml.safe_dump(_PROFILE), encoding="utf-8")
    (root / "assets" / "voice-guide.md").write_text(_VOICE, encoding="utf-8")
    cfg = JobsmithConfig()
    cfg.voice.voice_guide_path = Path("assets/voice-guide.md")
    cfg.llm.apply.orchestrator = "code_local"
    return cfg


def _bullet_ids(root: Path) -> dict[str, str]:
    work_path = root / "assets" / "content" / "work.yml"
    return {f"{b.position_index}.{b.bullet_index}": b.bullet_id for b in parse_master_bullets(work_path)}


# ---------------------------------------------------------------------------
# Canned payloads (valid, no-halt) keyed by json_schema name
# ---------------------------------------------------------------------------


def _jd_payload() -> dict:
    return {
        "company": "Helios Energy", "position": "Senior Data Engineer", "location": "Remote",
        "location_type": "remote", "salary_range": None, "req_id": None,
        "apply_url": "https://jobs.example.com/1", "named_hm": None, "role_type": "data-engineer",
        "must_haves": ["Python", "ETL pipelines"], "nice_to_haves": ["Dagster"],
        "top_keywords": ["python", "etl", "sql"], "jd_text_clean": "Need a data engineer.",
        "jd_url": "https://jobs.example.com/1",
    }


def _fit_payload() -> dict:
    return {
        "specialty": "tax_equity", "score_raw": 82, "rationale": "Strong match.",
        "matched_evidence": ["work.0.0"], "concerns": [], "confidence": "high",
        "must_have_table": [
            {"requirement": "Python", "level": "STRONG", "evidence": "geospatial Python"},
            {"requirement": "ETL pipelines", "level": "HAVE", "evidence": "pipelines"},
        ],
        "pitch": "A data engineer who unlocks regulatory value at scale.",
    }


def _bullet_payload(ids: dict[str, str]) -> dict:
    money, mentor, reporting, dictionary = ids["0.0"], ids["0.1"], ids["1.0"], ids["1.1"]

    def choice(bid: str) -> dict:
        return {"master_bullet_id": bid, "included": True, "rephrased": None, "reason_if_dropped": None}

    return {
        "positions": [
            {"company": "Helios Energy", "title": "Senior Data Engineer",
             "bullets": [choice(money), choice(mentor)]},
            {"company": "Atlas Capital", "title": "Data Analyst",
             "bullets": [choice(reporting), choice(dictionary)]},
        ],
        "anchor_bullets_master": [money, reporting],
        "anchor_bullets_kept": [money, reporting],
        "anchor_bullets_dropped": [],
        "uncovered_must_haves": [],
        "restoration_queue": {"bullets": [], "context_hash": ""},
    }


def _prose_md() -> str:
    return (
        "# Professional Summary\n\n"
        "Data engineer building renewable analytics platforms with Python and SQL.\n\n"
        "# Tailored Bullets\n\n## Senior Data Engineer @ Helios Energy\n"
        "- Cut quarterly report time from 5 days to 4 hours using Python automation\n"
        "- Mentored two analysts to handle business requests independently\n"
    )


def _prose_payload() -> dict:
    return {"markdown": _prose_md(), "would_fabricate": None}


class SchemaStub:
    """Returns a canned payload keyed on ``schema.json_schema.name``; records calls."""

    def __init__(self, payloads: dict[str, dict | None]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    def complete_structured(self, messages, schema, *, temperature=0.0):
        name = (schema.get("json_schema") or {}).get("name", "")
        self.calls.append(name)
        payload = self.payloads.get(name)
        return (payload, True) if payload is not None else (None, False)


def _full_stub(ids: dict[str, str]) -> SchemaStub:
    return SchemaStub({
        "jd_parsed": _jd_payload(),
        "fit_score": _fit_payload(),
        "bullet_selection": _bullet_payload(ids),
        "prose_draft": _prose_payload(),
    })


def _state(root: Path, name: str) -> Path:
    return root / "applications" / SLUG / ".apply-state" / name


# ===========================================================================
# Happy path + orchestration order (gather -> draft)
# ===========================================================================


def test_full_gather_to_draft_produces_all_artifacts(tmp_path: Path) -> None:
    cfg = _setup(tmp_path)
    backend = _full_stub(_bullet_ids(tmp_path))

    outcome = run_local_apply(jd_text="Need a data engineer.", slug=SLUG, config=cfg,
                              repo_root=tmp_path, backend=backend)

    assert outcome.status == OK and outcome.ok
    # jd-parsed.json ... prose-draft.md all written
    for name in (ART_JD_PARSED, ART_FIT_SCORE, ART_BULLET_SELECTION, ART_PROSE_DRAFT):
        assert _state(tmp_path, name).exists(), f"missing artifact {name}"
    assert ART_PROSE_DRAFT in outcome.artifacts
    body = _state(tmp_path, ART_PROSE_DRAFT).read_text(encoding="utf-8")
    assert "# Professional Summary" in body
    # order: gather schemas before the draft schema
    assert backend.calls.index("jd_parsed") < backend.calls.index("prose_draft")
    assert backend.calls[:3] == ["jd_parsed", "fit_score", "bullet_selection"]


def test_draft_context_loaded_from_gather_artifacts(tmp_path: Path) -> None:
    """The draft pipeline must see gather's on-disk artifacts as its context."""
    cfg = _setup(tmp_path)
    backend = _full_stub(_bullet_ids(tmp_path))
    run_local_apply(jd_text="x", slug=SLUG, config=cfg, repo_root=tmp_path, backend=backend)
    # bullet_selection.json written by gather then read for the draft step
    data = json.loads(_state(tmp_path, ART_BULLET_SELECTION).read_text())
    assert data["positions"]


# ===========================================================================
# Per-node routing flows through Pipeline.run (the wiring slice 6 deferred)
# ===========================================================================


def test_hybrid_routing_sends_prose_to_cloud_rest_local(tmp_path: Path) -> None:
    cfg = _setup(tmp_path)
    ids = _bullet_ids(tmp_path)
    local = SchemaStub({"jd_parsed": _jd_payload(), "fit_score": _fit_payload(),
                        "bullet_selection": _bullet_payload(ids)})
    cloud = SchemaStub({"prose_draft": _prose_payload()})
    routing = RoutingBackend({
        "jd_parsed": local, "fit_score": local, "bullet_selection": local, "prose_draft": cloud,
    })

    outcome = run_local_apply(jd_text="x", slug=SLUG, config=cfg, repo_root=tmp_path, backend=routing)

    assert outcome.ok
    assert cloud.calls == ["prose_draft"]  # prose-write routed to cloud
    assert "jd_parsed" in local.calls and "prose_draft" not in local.calls  # rest local


def test_build_routing_backend_resolves_per_node_provider(tmp_path: Path) -> None:
    from jobsmith.apply_local.nodes_gather import build_gather_pipeline
    from jobsmith.apply_local.nodes_draft import build_draft_pipeline

    cfg = _setup(tmp_path)
    cfg.llm.apply.node_backend = NodeBackendConfig(
        provider="openai_compatible", base_url="http://localhost:1234/v1"
    )
    cfg.llm.apply.node_backends[NODE_PROSE_WRITE] = NodeBackendConfig(
        provider="anthropic", model="claude-opus-4-1", api_key="sk-test"
    )
    gather = build_gather_pipeline(cfg, SLUG, root=tmp_path)
    draft = build_draft_pipeline(cfg, SLUG, root=tmp_path)

    routing = build_routing_backend(cfg, [gather, draft])
    assert isinstance(routing._by_name["prose_draft"], AnthropicBackend)
    assert isinstance(routing._by_name["jd_parsed"], OpenAICompatBackend)


def test_routing_backend_unknown_schema_raises() -> None:
    rb = RoutingBackend({"jd_parsed": SchemaStub({"jd_parsed": _jd_payload()})})
    with pytest.raises(RuntimeError, match="no backend resolved"):
        rb.complete_structured([], {"json_schema": {"name": "nope"}})


# ===========================================================================
# done_when 2 — ENGINE-DOWN detection + on_failure policy
# ===========================================================================


class _FailingClient:
    def complete(self, messages, response_format=None, temperature=0.0):
        raise ConnectionError("connection refused")


def _patch_engine(monkeypatch, *, health_state: str) -> None:
    monkeypatch.setattr(run_mod, "ensure_engine", lambda config: ("http://127.0.0.1:1/v1", True))
    monkeypatch.setattr(run_mod, "resolve_backend",
                        lambda config, node: OpenAICompatBackend(base_url="http://x/v1", _client=_FailingClient()))
    monkeypatch.setattr(vllm_mlx, "health", lambda **_: {"state": health_state, "base_url": None})
    monkeypatch.setattr(vllm_mlx, "stop", lambda **_: True)


def test_engine_crash_detected_as_engine_down_error_policy(tmp_path: Path, monkeypatch) -> None:
    cfg = _setup(tmp_path)
    cfg.llm.apply.on_failure = "error"
    _patch_engine(monkeypatch, health_state=vllm_mlx.STATE_CRASHED)

    outcome = run_local_apply(jd_text="x", slug=SLUG, config=cfg, repo_root=tmp_path)

    assert outcome.status == ENGINE_DOWN  # NOT misread as a node halt
    assert outcome.fallback_cloud is False  # on_failure=error -> caller surfaces error


def test_engine_crash_engine_down_fallback_cloud_policy(tmp_path: Path, monkeypatch) -> None:
    cfg = _setup(tmp_path)
    cfg.llm.apply.on_failure = "fallback_cloud"
    _patch_engine(monkeypatch, health_state=vllm_mlx.STATE_CRASHED)

    outcome = run_local_apply(jd_text="x", slug=SLUG, config=cfg, repo_root=tmp_path)

    assert outcome.status == ENGINE_DOWN
    assert outcome.fallback_cloud is True  # caller should route to claude -p


def test_node_halt_with_healthy_engine_is_not_engine_down(tmp_path: Path, monkeypatch) -> None:
    """A genuine model failure with a READY engine surfaces as HALT, not engine_down."""
    cfg = _setup(tmp_path)
    cfg.llm.apply.on_failure = "fallback_cloud"
    _patch_engine(monkeypatch, health_state=vllm_mlx.STATE_READY)

    outcome = run_local_apply(jd_text="x", slug=SLUG, config=cfg, repo_root=tmp_path)

    assert outcome.status == HALT  # engine healthy -> the failure is the model's
    assert outcome.fallback_cloud is False


def test_engine_not_installed_surfaces_engine_down(tmp_path: Path, monkeypatch) -> None:
    cfg = _setup(tmp_path)
    cfg.llm.apply.node_backend = NodeBackendConfig(
        provider="openai_compatible", base_url="http://localhost:9/v1"
    )

    def _raise(config):
        raise EngineUnavailableError("vllm-mlx not installed: uv pip install vllm-mlx")

    monkeypatch.setattr(run_mod, "ensure_engine", _raise)
    outcome = run_local_apply(jd_text="x", slug=SLUG, config=cfg, repo_root=tmp_path)
    assert outcome.status == ENGINE_DOWN
    assert "vllm-mlx" in (outcome.reason or "")


def test_ensure_engine_reuses_live_configured_endpoint(tmp_path: Path, monkeypatch) -> None:
    cfg = _setup(tmp_path)
    cfg.llm.apply.node_backend = NodeBackendConfig(
        provider="openai_compatible", base_url="http://127.0.0.1:8081/v1"
    )
    monkeypatch.setattr(vllm_mlx, "_models_ready", lambda port, **_: port == 8081)
    base_url, managed = ensure_engine(cfg)
    assert base_url == "http://127.0.0.1:8081/v1" and managed is False


# ===========================================================================
# done_when 3 — claude_cloud passthrough unchanged; code_local routes local
# ===========================================================================


def test_claude_cloud_default_routes_to_cloud_path(tmp_path: Path, monkeypatch) -> None:
    import jobsmith._cli_apply as cli
    import jobsmith.core.pipeline as pipeline

    cfg = JobsmithConfig()  # default orchestrator == claude_cloud
    monkeypatch.setattr(cli, "load_config", lambda **_: cfg)
    called = {"cloud": False, "local": False}

    def _fake_core(*a, **k):
        called["cloud"] = True
        return 0

    monkeypatch.setattr(pipeline, "core_run_apply", _fake_core)
    monkeypatch.setattr(cli, "_run_code_local_apply",
                        lambda *a, **k: called.__setitem__("local", True))

    rc = cli.run_apply("https://jobs.example.com/1", cwd=tmp_path, jd_text="x")
    assert rc == 0
    assert called["cloud"] is True and called["local"] is False


def test_code_local_routes_to_local_and_skips_cloud(tmp_path: Path, monkeypatch) -> None:
    import jobsmith._cli_apply as cli
    import jobsmith.core.pipeline as pipeline

    cfg = JobsmithConfig()
    cfg.llm.apply.orchestrator = "code_local"
    monkeypatch.setattr(cli, "load_config", lambda **_: cfg)
    called = {"cloud": False}
    monkeypatch.setattr(pipeline, "core_run_apply", lambda *a, **k: called.__setitem__("cloud", True) or 0)
    monkeypatch.setattr(cli, "_run_code_local_apply",
                        lambda *a, **k: ApplyOutcome(status=OK, slug=SLUG))

    rc = cli.run_apply("https://jobs.example.com/1", cwd=tmp_path, jd_text="x")
    assert rc == 0
    assert called["cloud"] is False  # cloud path NOT taken


def test_code_local_engine_down_error_returns_nonzero(tmp_path: Path, monkeypatch) -> None:
    import jobsmith._cli_apply as cli
    import jobsmith.core.pipeline as pipeline

    cfg = JobsmithConfig()
    cfg.llm.apply.orchestrator = "code_local"
    monkeypatch.setattr(cli, "load_config", lambda **_: cfg)
    monkeypatch.setattr(pipeline, "core_run_apply", lambda *a, **k: 0)
    monkeypatch.setattr(cli, "_run_code_local_apply",
                        lambda *a, **k: ApplyOutcome(status=ENGINE_DOWN, slug=SLUG, reason="down",
                                                     fallback_cloud=False))
    rc = cli.run_apply("https://jobs.example.com/1", cwd=tmp_path, jd_text="x")
    assert rc == 1


def test_code_local_fallback_cloud_falls_through(tmp_path: Path, monkeypatch) -> None:
    import jobsmith._cli_apply as cli
    import jobsmith.core.pipeline as pipeline

    cfg = JobsmithConfig()
    cfg.llm.apply.orchestrator = "code_local"
    monkeypatch.setattr(cli, "load_config", lambda **_: cfg)
    called = {"cloud": False}
    monkeypatch.setattr(pipeline, "core_run_apply", lambda *a, **k: called.__setitem__("cloud", True) or 0)
    monkeypatch.setattr(cli, "_run_code_local_apply",
                        lambda *a, **k: ApplyOutcome(status=ENGINE_DOWN, slug=SLUG, reason="down",
                                                     fallback_cloud=True))
    rc = cli.run_apply("https://jobs.example.com/1", cwd=tmp_path, jd_text="x")
    assert rc == 0
    assert called["cloud"] is True  # fell through to the cloud path
