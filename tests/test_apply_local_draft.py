"""Tests for the DRAFT specialists of the code-orchestrated LOCAL apply path
(feat-9517bed8, slice 6).

These exercise the prose-write Node, the prose-qa DETERMINISTIC five checks, the
bounded prose-write<->prose-qa revise loop, and the no-fabrication HALT. No live
model: a STUB backend returns canned ``prose-draft`` payloads (markdown carried
in a one-field JSON envelope so the structured transport still applies). Sample
(Pat-Doe-style) master + voice data is written under a tmp_path repo root; the
real user's data is never read.

done_when proven here:
  1. prose-write produces prose-draft.md with a non-empty Professional Summary,
     built from inputs.py-assembled master + voice context, and can be routed
     per-node to cloud Claude via llm.apply.node_backends.
  2. prose-qa runs the five blocking checks in code (each flags AND clears its
     pattern) and returns pass|revise; the driver loops prose-write<->prose-qa at
     most 3 times, then surfaces the remaining blocking findings as a halt.
  3. A would-fabricate situation in prose-write returns status=halt and NO
     prose-draft.md / ai-tell-report.json is ever written.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from jobsmith.apply_local.backends import AnthropicBackend, OpenAICompatBackend, resolve_backend
from jobsmith.apply_local.checkpoint import apply_state_dir
from jobsmith.apply_local.inputs import build_prose_write_prompt, load_master_data, load_voice_guide
from jobsmith.apply_local.nodes_draft import (
    ART_AI_TELL_REPORT,
    ART_PROSE_DRAFT,
    MAX_DRAFT_ITERS,
    NODE_PROSE_WRITE,
    ProseDraft,
    build_draft_pipeline,
    run_prose_qa_checks,
)
from jobsmith.config import JobsmithConfig, NodeBackendConfig

SLUG = "helios-data-engineer"
EM = "—"  # em dash

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
]
_SKILL = [{"title": "Programming", "description": "Python, SQL", "details": ["Python", "SQL"]}]
_EDUCATION = [
    {"title": "Northeastern University", "location": "Boston, MA", "date": "2018 - 2020",
     "description": "M.S. Data Analytics Engineering", "details": ["Thesis: geospatial ML"]}
]
_AUTHOR = {"author": [{"name": {"first": "Pat", "last": "Doe"}, "email": "pat@example.com"}]}
_PROFILE = {"user": {"name": "Pat Doe"}, "years": {"total_quantitative": 6}}
_VOICE_GUIDE = "Explorer not marketer. Thesis not product. Specific not sweeping."


def _setup(root: Path) -> JobsmithConfig:
    """Write master YAMLs + profile + voice guide under a default-layout root."""
    content = root / "assets" / "content"
    content.mkdir(parents=True, exist_ok=True)
    (content / "work.yml").write_text(yaml.safe_dump(_WORK), encoding="utf-8")
    (content / "skill.yml").write_text(yaml.safe_dump(_SKILL), encoding="utf-8")
    (content / "education.yml").write_text(yaml.safe_dump(_EDUCATION), encoding="utf-8")
    (content / "author.yml").write_text(yaml.safe_dump(_AUTHOR), encoding="utf-8")
    cap = root / "private" / "capacity"
    cap.mkdir(parents=True, exist_ok=True)
    (cap / "profile.yaml").write_text(yaml.safe_dump(_PROFILE), encoding="utf-8")
    (root / "assets" / "voice-guide.md").write_text(_VOICE_GUIDE, encoding="utf-8")
    cfg = JobsmithConfig()
    cfg.voice.voice_guide_path = Path("assets/voice-guide.md")
    return cfg


# ---------------------------------------------------------------------------
# Markdown fixtures — one clean draft + one bullet per isolated violation
# ---------------------------------------------------------------------------

_CLEAN_SUMMARY = "Data engineer building renewable analytics platforms with Python and SQL."
_CLEAN_BULLETS = [
    "Cut quarterly report time from 5 days to 4 hours using Python automation",
    "Mentored two analysts to handle business requests independently",
]
_BAD_WORD_COUNT = (
    "Designed and maintained reporting workflows that summarize quarterly figures for "
    "analysts and managers so they can review trends and plan resourcing decisions "
    "efficiently across the organization each month"
)
_BAD_METRIC = "Managed 5 teams across 12 regions delivering 30 projects yearly"
_BAD_PARENS = "Built ingestion service for analytics workloads (Python, DuckDB, Dagster)"
_BAD_EM_DASH = f"Refined the data model {EM} improving query clarity for analysts"
_BAD_STOCK = "Leveraged existing pipelines to speed up analyst reporting"


def _draft_md(summary: str, bullets: list[str]) -> str:
    lines = ["# Professional Summary", "", summary, "", "# Tailored Bullets", "",
             "## Senior Data Engineer @ Helios Energy"]
    lines += [f"- {b}" for b in bullets]
    return "\n".join(lines) + "\n"


def _clean_md() -> str:
    return _draft_md(_CLEAN_SUMMARY, _CLEAN_BULLETS)


def _prose_payload(markdown: str, would_fabricate: str | None = None) -> dict:
    return {"markdown": markdown, "would_fabricate": would_fabricate}


# ---------------------------------------------------------------------------
# Stub backend — returns a queued/fixed ``prose_draft`` payload
# ---------------------------------------------------------------------------


class StubBackend:
    """Returns ``prose_draft`` payloads: a per-call queue, else a fixed payload.

    Satisfies the driver's StructuredBackend protocol. ``calls`` counts every
    structured completion so iteration counts can be asserted.
    """

    def __init__(self, *, fixed: dict | None = None, queue: list[dict] | None = None) -> None:
        self.fixed = fixed
        self.queue = list(queue or [])
        self.calls = 0

    def complete_structured(self, messages, schema, *, temperature=0.0):
        self.calls += 1
        payload = self.queue.pop(0) if self.queue else self.fixed
        if payload is None:
            return None, False
        return payload, True


def _ctx() -> dict:
    return {"jd_parsed": {"must_haves": ["Python", "ETL pipelines"]}, "fit_score": {}, "bullet_selection": {}}


# ===========================================================================
# done_when 1 — prose-write produces prose-draft.md with a non-empty summary
# ===========================================================================


def test_prose_write_writes_draft_with_professional_summary(tmp_path: Path) -> None:
    cfg = _setup(tmp_path)
    backend = StubBackend(fixed=_prose_payload(_clean_md()))
    pipeline = build_draft_pipeline(cfg, SLUG, root=tmp_path)

    result = pipeline.run(backend, context=_ctx())

    assert result.status == "ok"
    draft = apply_state_dir(SLUG, root=tmp_path) / ART_PROSE_DRAFT
    assert draft.exists()
    body = draft.read_text(encoding="utf-8")
    assert "# Professional Summary" in body
    summary = body.split("# Professional Summary", 1)[1].split("#", 1)[0].strip()
    assert summary, "Professional Summary section must be non-empty"
    report = json.loads((apply_state_dir(SLUG, root=tmp_path) / ART_AI_TELL_REPORT).read_text())
    assert report["decision"] == "pass"


def test_prose_write_routes_per_node_to_cloud_claude(tmp_path: Path) -> None:
    cfg = _setup(tmp_path)
    cfg.llm.apply.node_backends[NODE_PROSE_WRITE] = NodeBackendConfig(
        provider="anthropic", model="claude-opus-4-1", api_key="sk-test"
    )
    assert isinstance(resolve_backend(cfg, NODE_PROSE_WRITE), AnthropicBackend)

    cfg2 = _setup(tmp_path)
    cfg2.llm.apply.node_backend = NodeBackendConfig(
        provider="openai_compatible", base_url="http://localhost:1234/v1"
    )
    assert isinstance(resolve_backend(cfg2, NODE_PROSE_WRITE), OpenAICompatBackend)


def test_prose_write_prompt_uses_master_and_voice_context(tmp_path: Path) -> None:
    cfg = _setup(tmp_path)
    master = load_master_data(cfg, repo_root=tmp_path)
    guide = load_voice_guide(cfg, repo_root=tmp_path)
    assert guide == _VOICE_GUIDE

    prompt = build_prose_write_prompt(
        master,
        {"must_haves": ["Python", "ETL pipelines"]},
        {"must_have_table": []},
        {"positions": []},
        voice_guide=guide,
    )
    assert "tax credits" in prompt  # a master work-bullet fact
    assert "Explorer not marketer" in prompt  # voice guide
    assert "Python" in prompt  # a JD must-have


def test_load_voice_guide_missing_returns_empty(tmp_path: Path) -> None:
    cfg = JobsmithConfig()  # no voice_guide_path set
    assert load_voice_guide(cfg, repo_root=tmp_path) == ""


# ===========================================================================
# done_when 2 — the five deterministic checks each flag AND clear; loop bounded
# ===========================================================================


def _violations(markdown: str, key: str) -> int:
    report = run_prose_qa_checks(markdown, iteration=1, max_iter=MAX_DRAFT_ITERS)
    return report["bullet_style_checks"][key]["violations"]


def test_check_bullet_word_count_flags_and_clears() -> None:
    assert _violations(_draft_md(_CLEAN_SUMMARY, [_BAD_WORD_COUNT]), "bullet_word_count") >= 1
    assert _violations(_clean_md(), "bullet_word_count") == 0


def test_check_metric_cluster_flags_and_clears() -> None:
    assert _violations(_draft_md(_CLEAN_SUMMARY, [_BAD_METRIC]), "metric_cluster_count") >= 1
    # the clean baseline-pair bullet ("5 days to 4 hours") is ONE cluster, not a violation
    assert _violations(_clean_md(), "metric_cluster_count") == 0


def test_check_parenthetical_tech_list_flags_and_clears() -> None:
    assert _violations(_draft_md(_CLEAN_SUMMARY, [_BAD_PARENS]), "parenthetical_tech_list") >= 1
    assert _violations(_clean_md(), "parenthetical_tech_list") == 0


def test_check_em_dash_flags_and_clears() -> None:
    assert _violations(_draft_md(_CLEAN_SUMMARY, [_BAD_EM_DASH]), "em_dash") >= 1
    assert _violations(_clean_md(), "em_dash") == 0


def test_check_stock_phrases_flags_and_clears() -> None:
    assert _violations(_draft_md(_CLEAN_SUMMARY, [_BAD_STOCK]), "stock_phrases") >= 1
    assert _violations(_clean_md(), "stock_phrases") == 0


def test_qa_decision_pass_revise_halt() -> None:
    clean = run_prose_qa_checks(_clean_md(), iteration=1, max_iter=3)
    assert clean["decision"] == "pass"
    revise = run_prose_qa_checks(_draft_md(_CLEAN_SUMMARY, [_BAD_STOCK]), iteration=1, max_iter=3)
    assert revise["decision"] == "revise"
    assert any(f["category"] == "stock_phrases" for f in revise["blocking_findings"])
    halt = run_prose_qa_checks(_draft_md(_CLEAN_SUMMARY, [_BAD_STOCK]), iteration=3, max_iter=3)
    assert halt["decision"] == "halt"


def test_revise_loop_converges_after_one_revision(tmp_path: Path) -> None:
    cfg = _setup(tmp_path)
    backend = StubBackend(queue=[
        _prose_payload(_draft_md(_CLEAN_SUMMARY, [_BAD_STOCK])),  # iter 1 -> revise
        _prose_payload(_clean_md()),                              # iter 2 -> pass
    ])
    result = build_draft_pipeline(cfg, SLUG, root=tmp_path).run(backend, context=_ctx())

    assert result.status == "ok"
    assert backend.calls == 2
    report = json.loads((apply_state_dir(SLUG, root=tmp_path) / ART_AI_TELL_REPORT).read_text())
    assert report["decision"] == "pass"


def test_revise_loop_bounded_at_three_then_halts(tmp_path: Path) -> None:
    cfg = _setup(tmp_path)
    backend = StubBackend(fixed=_prose_payload(_draft_md(_CLEAN_SUMMARY, [_BAD_STOCK])))
    result = build_draft_pipeline(cfg, SLUG, root=tmp_path).run(backend, context=_ctx())

    assert result.status == "halt"
    assert backend.calls == MAX_DRAFT_ITERS == 3
    state = apply_state_dir(SLUG, root=tmp_path)
    # draft is left on disk for manual review; the report records the halt
    assert (state / ART_PROSE_DRAFT).exists()
    report = json.loads((state / ART_AI_TELL_REPORT).read_text())
    assert report["decision"] == "halt"
    assert report["blocking_findings"], "remaining blocking findings must be surfaced"
    assert result.reason and "stock_phrase" in result.reason


# ===========================================================================
# done_when 3 — would-fabricate halts and writes NOTHING
# ===========================================================================


def test_would_fabricate_halts_without_writing_draft(tmp_path: Path) -> None:
    cfg = _setup(tmp_path)
    claim = "$999M in fabricated cost savings"
    backend = StubBackend(fixed=_prose_payload(_clean_md(), would_fabricate=claim))
    result = build_draft_pipeline(cfg, SLUG, root=tmp_path).run(backend, context=_ctx())

    assert result.status == "halt"
    assert result.reason and "WOULD_FABRICATE" in result.reason and claim in result.reason
    assert backend.calls == 1  # halts on the first write; no revise loop
    state = apply_state_dir(SLUG, root=tmp_path)
    assert not (state / ART_PROSE_DRAFT).exists()
    assert not (state / ART_AI_TELL_REPORT).exists()


def test_prose_draft_model_carries_markdown_and_fabricate_signal() -> None:
    obj = ProseDraft.model_validate({"markdown": "# Professional Summary\n\nHi.", "would_fabricate": None})
    assert obj.markdown.startswith("# Professional Summary")
    assert obj.would_fabricate is None


# ===========================================================================
# FIX 1 — empty/blank draft guard in run_prose_qa_checks
# ===========================================================================


@pytest.mark.parametrize("empty", ["", "   ", "\n\t\n"])
def test_empty_draft_is_never_pass(empty: str) -> None:
    """An empty or whitespace-only draft must never return decision='pass'."""
    report = run_prose_qa_checks(empty, iteration=1, max_iter=MAX_DRAFT_ITERS)
    assert report["decision"] != "pass", "empty draft must not pass qa"
    assert any(f["category"] == "empty_draft" for f in report["blocking_findings"])


def test_empty_draft_decision_is_revise_before_max_iter() -> None:
    report = run_prose_qa_checks("", iteration=1, max_iter=3)
    assert report["decision"] == "revise"


def test_empty_draft_decision_is_halt_at_max_iter() -> None:
    report = run_prose_qa_checks("", iteration=3, max_iter=3)
    assert report["decision"] == "halt"


def test_empty_draft_loop_halts_not_passes(tmp_path: Path) -> None:
    """Pipeline with a prose-write that always returns empty markdown must HALT, not pass."""
    cfg = _setup(tmp_path)
    # StubBackend always returns parse_ok=False (None, False) for empty content;
    # simulate: return a ProseDraft with markdown="" which triggers FIX 1.
    backend = StubBackend(fixed=_prose_payload(""))
    result = build_draft_pipeline(cfg, SLUG, root=tmp_path).run(backend, context=_ctx())

    assert result.status == "halt", "pipeline must HALT on repeated empty draft, never 'ok'"
    assert backend.calls == MAX_DRAFT_ITERS
