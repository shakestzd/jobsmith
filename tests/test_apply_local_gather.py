"""Tests for the GATHER specialists of the code-orchestrated LOCAL apply path
(feat-d46dde68, slice 5).

These exercise three code Nodes over the existing
``applications/{slug}/.apply-state`` contract — jd-parse, fit-score, and
bullet-select — plus the master-data assembly (inputs.py) and the v1
no-fabrication HALT policy. No live model: a STUB backend returns canned
payloads keyed on each node's json_schema name. Sample (Pat-Doe-style) master
data is written under a tmp_path repo root; the real user's data is never read.

done_when proven here:
  1. inputs.py loads each gather node's DECLARED master data (profile + master
     YAMLs) and injects it; a missing REQUIRED master file surfaces a clear
     error, not a silent empty prompt.
  2. jd-parse / fit-score / bullet-select each produce their declared
     .apply-state artifacts (jd-parsed.json with company/position/role_type +
     must_haves>=2; fit-score.json normalized 0-1; bullet-selection.json +
     bullet-diff.md + bullet-decisions.json), validated against pydantic schemas.
  3. When a JD must-have has no master coverage OR an anchor bullet would drop
     without a logged reason, the node returns status=halt with the reason, and
     the pipeline surfaces it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from jobsmith.apply_local.checkpoint import apply_state_dir
from jobsmith.apply_local.inputs import (
    NODE_MASTER_REQUIREMENTS,
    MissingMasterDataError,
    load_master_data,
    load_node_master,
)
from jobsmith.apply_local.nodes_gather import (
    NODE_BULLET_SELECT,
    NODE_FIT_SCORE,
    NODE_JD_PARSE,
    build_gather_pipeline,
)
from jobsmith.apply_local.schemas import (
    ART_BULLET_DECISIONS,
    ART_BULLET_DIFF,
    ART_BULLET_SELECTION,
    ART_FIT_SCORE,
    ART_JD_PARSED,
    BulletDecisions,
    BulletSelection,
    FitScore,
    JdParsed,
)
from jobsmith.config import JobsmithConfig
from jobsmith.guard import parse_master_bullets

SLUG = "helios-data-engineer"


# ---------------------------------------------------------------------------
# Sample master data (Pat-Doe-style fixtures — NOT the real user's content)
# ---------------------------------------------------------------------------

_WORK = [
    {
        "title": "Senior Data Engineer",
        "location": "Helios Energy",
        "date": "2024 - Present",
        "description": "Remote",
        "details": [
            "Unlocked $250M in tax credits across 200K solar assets via geospatial Python",
            "Mentored 2 analysts to handle business requests independently",
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
                "anchor_reason": "Story-of-impact bullet for ops-leaning JDs",
                "tags": ["reporting"],
            },
            "Co-designed a PostgreSQL data dictionary adopted across three teams",
        ],
    },
]

_SKILL = [
    {"title": "Programming", "description": "Python, SQL", "details": ["Python", "SQL"]},
    {"title": "Data Engineering", "description": "Dagster, DuckDB", "details": ["Dagster", "DuckDB"]},
]

_EDUCATION = [
    {
        "title": "Northeastern University",
        "location": "Boston, MA",
        "date": "2018 - 2020",
        "description": "M.S. Data Analytics Engineering",
        "details": ["Thesis: geospatial ML for renewable siting"],
    }
]

_AUTHOR = {
    "author": [
        {
            "name": {"first": "Pat", "last": "Doe"},
            "email": "pat@example.com",
            "position": "Data Engineering | Renewable Analytics",
        }
    ]
}

_PUBLICATION = [
    {"title": "Geospatial ML for siting", "authors": ["Doe, P."], "venue": "Applied Energy", "year": 2021}
]

_PROFILE = {
    "user": {"name": "Pat Doe"},
    "stack": {"python_advanced": True, "sql_advanced": True},
    "years": {"total_quantitative": 6},
}


def _write_master(root: Path) -> JobsmithConfig:
    """Write all master YAMLs + profile under a default-layout repo root."""
    content = root / "assets" / "content"
    content.mkdir(parents=True, exist_ok=True)
    (content / "work.yml").write_text(yaml.safe_dump(_WORK), encoding="utf-8")
    (content / "skill.yml").write_text(yaml.safe_dump(_SKILL), encoding="utf-8")
    (content / "education.yml").write_text(yaml.safe_dump(_EDUCATION), encoding="utf-8")
    (content / "author.yml").write_text(yaml.safe_dump(_AUTHOR), encoding="utf-8")
    pub = content / "publication.yml"
    pub.write_text(yaml.safe_dump(_PUBLICATION), encoding="utf-8")
    cap = root / "private" / "capacity"
    cap.mkdir(parents=True, exist_ok=True)
    (cap / "profile.yaml").write_text(yaml.safe_dump(_PROFILE), encoding="utf-8")
    cfg = JobsmithConfig()
    cfg.master.publication_yml = Path("assets/content/publication.yml")
    return cfg


def _bullet_ids(root: Path) -> dict[str, str]:
    """Map ``"{position}.{bullet}"`` -> master_bullet_id for building selections."""
    work_path = root / "assets" / "content" / "work.yml"
    return {f"{b.position_index}.{b.bullet_index}": b.bullet_id for b in parse_master_bullets(work_path)}


# ---------------------------------------------------------------------------
# Stub backend — routes a canned payload by the node's json_schema name
# ---------------------------------------------------------------------------


class RoutingBackend:
    """Returns a canned payload keyed on ``schema.json_schema.name``.

    A name mapped to ``None`` (or absent) yields ``(None, False)`` so the
    driver's reask loop engages. ``per_call`` overrides a name with a queue of
    payloads consumed in order (for reask tests).
    """

    def __init__(self, payloads: dict[str, dict | None], *, per_call: dict[str, list] | None = None):
        self.payloads = payloads
        self.per_call = {k: list(v) for k, v in (per_call or {}).items()}
        self.calls: list[str] = []

    def complete_structured(self, messages, schema, *, temperature=0.0):
        name = (schema.get("json_schema") or {}).get("name", "")
        self.calls.append(name)
        if name in self.per_call and self.per_call[name]:
            payload = self.per_call[name].pop(0)
        else:
            payload = self.payloads.get(name)
        if payload is None:
            return None, False
        return payload, True


def _jd_payload(must_haves: list[str] | None = None) -> dict:
    return {
        "company": "Helios Energy",
        "position": "Senior Data Engineer",
        "location": "Remote",
        "location_type": "remote",
        "salary_range": None,
        "req_id": None,
        "apply_url": "https://jobs.example.com/123",
        "named_hm": None,
        "role_type": "data-engineer",
        "must_haves": must_haves if must_haves is not None else ["Python", "ETL pipelines"],
        "nice_to_haves": ["Dagster"],
        "top_keywords": ["python", "etl", "duckdb", "dagster", "sql"],
        "jd_text_clean": "We need a data engineer with Python and ETL experience.",
        "jd_url": "https://jobs.example.com/123",
    }


def _fit_payload(*, level: str = "HAVE", score_raw: int = 82) -> dict:
    return {
        "specialty": "tax_equity",
        "score_raw": score_raw,
        "rationale": "Strong ETL + renewable analytics match.",
        "matched_evidence": ["work.0.0"],
        "concerns": [],
        "confidence": "high",
        "must_have_table": [
            {"requirement": "Python", "level": "STRONG", "evidence": "Built geospatial Python platform"},
            {"requirement": "ETL pipelines", "level": level, "evidence": "7 automated pipelines"},
        ],
        "pitch": "A data engineer who unlocks regulatory value at scale.",
    }


def _bullet_payload(ids: dict[str, str], *, drop_anchor: str | None = None, drop_reason: str | None = None,
                    uncovered: list[str] | None = None) -> dict:
    money = ids["0.0"]
    mentor = ids["0.1"]
    reporting = ids["1.0"]
    dictionary = ids["1.1"]

    def choice(bid: str, included: bool, reason: str | None = None) -> dict:
        return {"master_bullet_id": bid, "included": included, "rephrased": None, "reason_if_dropped": reason}

    money_inc = drop_anchor != "money"
    reporting_inc = drop_anchor != "reporting"
    dropped: list[dict] = []
    if drop_anchor == "money" and drop_reason:
        dropped.append({"bullet_id": money, "reason": drop_reason, "JD_keyword_replacing_it": "ETL"})
    money_choice = choice(money, money_inc, drop_reason if (drop_anchor == "money") else None)
    reporting_choice = choice(reporting, reporting_inc, drop_reason if (drop_anchor == "reporting") else None)

    kept_anchors = []
    if money_inc:
        kept_anchors.append(money)
    if reporting_inc:
        kept_anchors.append(reporting)
    return {
        "positions": [
            {
                "company": "Helios Energy",
                "title": "Senior Data Engineer",
                "bullets": [money_choice, choice(mentor, True)],
            },
            {
                "company": "Atlas Capital",
                "title": "Data Analyst",
                "bullets": [reporting_choice, choice(dictionary, True)],
            },
        ],
        "anchor_bullets_master": [money, reporting],
        "anchor_bullets_kept": kept_anchors,
        "anchor_bullets_dropped": dropped,
        "uncovered_must_haves": uncovered or [],
        "restoration_queue": {"bullets": [], "context_hash": ""},
    }


# ===========================================================================
# done_when 1 — inputs.py master-data assembly + missing-required error
# ===========================================================================


def test_load_master_data_loads_all_declared_sections(tmp_path: Path):
    cfg = _write_master(tmp_path)
    master = load_master_data(cfg, repo_root=tmp_path)
    assert master.profile["user"]["name"] == "Pat Doe"
    assert len(master.work) == 2
    assert len(master.skill) == 2
    assert master.education and master.publication
    assert master.author  # author block present
    assert len(master.work_bullets) == 4  # 2 positions x 2 bullets


def test_load_node_master_loads_only_declared(tmp_path: Path):
    cfg = _write_master(tmp_path)
    # jd-parse declares NO master data
    assert NODE_MASTER_REQUIREMENTS[NODE_JD_PARSE].required == ()
    jd = load_node_master(NODE_JD_PARSE, cfg, repo_root=tmp_path)
    assert jd.work == [] and jd.profile == {}
    # fit-score declares profile + work + skill + education + author
    fit = load_node_master(NODE_FIT_SCORE, cfg, repo_root=tmp_path)
    assert fit.profile and fit.work and fit.skill and fit.education and fit.author
    # bullet-select declares work + skill only
    bsel = load_node_master(NODE_BULLET_SELECT, cfg, repo_root=tmp_path)
    assert bsel.work and bsel.skill
    assert bsel.profile == {}  # not declared -> not loaded


def test_missing_required_master_file_raises_clear_error(tmp_path: Path):
    cfg = _write_master(tmp_path)
    (tmp_path / "assets" / "content" / "work.yml").unlink()
    with pytest.raises(MissingMasterDataError) as exc:
        load_node_master(NODE_BULLET_SELECT, cfg, repo_root=tmp_path)
    msg = str(exc.value)
    assert "work" in msg and "work.yml" in msg  # names the section + path


def test_missing_required_profile_for_fit_score_raises(tmp_path: Path):
    cfg = _write_master(tmp_path)
    (tmp_path / "private" / "capacity" / "profile.yaml").unlink()
    with pytest.raises(MissingMasterDataError) as exc:
        load_node_master(NODE_FIT_SCORE, cfg, repo_root=tmp_path)
    assert "profile" in str(exc.value)


def test_optional_publication_absent_is_not_an_error(tmp_path: Path):
    cfg = _write_master(tmp_path)
    (tmp_path / "assets" / "content" / "publication.yml").unlink()
    cfg.master.publication_yml = None
    master = load_master_data(cfg, repo_root=tmp_path)  # must NOT raise
    assert master.publication in (None, [])


def test_fit_score_prompt_injects_master_evidence(tmp_path: Path):
    from jobsmith.apply_local.inputs import build_fit_score_prompt

    cfg = _write_master(tmp_path)
    master = load_node_master(NODE_FIT_SCORE, cfg, repo_root=tmp_path)
    prompt = build_fit_score_prompt(master, _jd_payload(), fast_path_scores={"fast_score": 0.7})
    assert "$250M" in prompt  # master work evidence injected, not an empty prompt
    assert "Helios Energy" in prompt  # JD context injected


# ===========================================================================
# done_when 2 — each node writes its declared artifacts (schema-validated)
# ===========================================================================


def _state(root: Path, name: str) -> Path:
    return apply_state_dir(SLUG, root=root) / name


def _docs(root: Path, name: str) -> Path:
    return apply_state_dir(SLUG, root=root).parent / "documents" / name


def _setup(tmp_path: Path) -> tuple[JobsmithConfig, dict[str, str]]:
    """Write sample master data and return (config, bullet-id map)."""
    cfg = _write_master(tmp_path)
    return cfg, _bullet_ids(tmp_path)


def _run(cfg: JobsmithConfig, tmp_path: Path, backend: RoutingBackend):
    pipeline = build_gather_pipeline(cfg, SLUG, root=tmp_path)
    return pipeline.run(backend, context={"jd_text": "Need a data engineer.", "jd_url": None})


def test_jd_parse_writes_jd_parsed_artifact(tmp_path: Path):
    cfg, ids = _setup(tmp_path)
    backend = RoutingBackend({"jd_parsed": _jd_payload(), "fit_score": _fit_payload(),
                              "bullet_selection": _bullet_payload(ids)})
    result = _run(cfg, tmp_path, backend)
    assert result.status == "ok"
    path = _state(tmp_path, ART_JD_PARSED)
    assert path.exists()
    data = json.loads(path.read_text())
    jd = JdParsed.model_validate(data)
    assert jd.company == "Helios Energy" and jd.position == "Senior Data Engineer"
    assert jd.role_type == "data-engineer"
    assert len(jd.must_haves) >= 2


def test_jd_parse_must_haves_below_two_reasks_then_halts(tmp_path: Path):
    cfg, _ = _setup(tmp_path)
    # Always returns a single must-have -> fails JdParsed min_length -> reask -> halt
    backend = RoutingBackend({"jd_parsed": _jd_payload(must_haves=["only one"])})
    result = _run(cfg, tmp_path, backend)
    assert result.status == "halt"
    assert result.halt_node == NODE_JD_PARSE
    assert backend.calls.count("jd_parsed") >= 2  # reasked at least once


def test_jd_parse_recovers_after_one_bad_must_haves(tmp_path: Path):
    cfg, ids = _setup(tmp_path)
    backend = RoutingBackend(
        {"jd_parsed": _jd_payload(), "fit_score": _fit_payload(),
         "bullet_selection": _bullet_payload(ids)},
        per_call={"jd_parsed": [_jd_payload(must_haves=["only one"]), _jd_payload()]},
    )
    result = _run(cfg, tmp_path, backend)
    assert result.status == "ok"
    assert backend.calls.count("jd_parsed") == 2  # one bad, one good


def test_fit_score_writes_normalized_artifact(tmp_path: Path):
    cfg, ids = _setup(tmp_path)
    backend = RoutingBackend({"jd_parsed": _jd_payload(), "fit_score": _fit_payload(score_raw=82),
                              "bullet_selection": _bullet_payload(ids)})
    result = _run(cfg, tmp_path, backend)
    assert result.status == "ok"
    path = _state(tmp_path, ART_FIT_SCORE)
    assert path.exists()
    fit = FitScore.model_validate(json.loads(path.read_text()))
    assert fit.score_raw == 82
    assert fit.score == pytest.approx(0.82)  # normalized 0-100 -> 0-1
    assert 0.0 <= fit.score <= 1.0


def test_bullet_select_writes_all_companion_artifacts(tmp_path: Path):
    cfg, ids = _setup(tmp_path)
    backend = RoutingBackend({"jd_parsed": _jd_payload(), "fit_score": _fit_payload(),
                              "bullet_selection": _bullet_payload(ids)})
    result = _run(cfg, tmp_path, backend)
    assert result.status == "ok"
    sel_path = _state(tmp_path, ART_BULLET_SELECTION)
    diff_path = _state(tmp_path, ART_BULLET_DIFF)
    dec_path = _state(tmp_path, ART_BULLET_DECISIONS)
    assert sel_path.exists() and diff_path.exists() and dec_path.exists()
    sel = BulletSelection.model_validate(json.loads(sel_path.read_text()))
    assert len(sel.positions) == 2
    BulletDecisions.model_validate(json.loads(dec_path.read_text()))
    assert "Anchor bullet diff" in diff_path.read_text()
    # tailored work.yml + skill.yml under documents/, anchors preserved
    work = yaml.safe_load(_docs(tmp_path, "work.yml").read_text())
    assert _docs(tmp_path, "skill.yml").exists()
    flat = json.dumps(work)
    assert "$250M" in flat  # money anchor preserved in tailored work


def test_bullet_decisions_propagate_anchor_reason(tmp_path: Path):
    cfg, ids = _setup(tmp_path)
    # Drop the explicit-anchor reporting bullet WITH a logged reason
    backend = RoutingBackend({"jd_parsed": _jd_payload(), "fit_score": _fit_payload(),
                              "bullet_selection": _bullet_payload(ids, drop_anchor="reporting",
                                                                  drop_reason="JD is finance-lite")})
    result = _run(cfg, tmp_path, backend)
    assert result.status == "ok"  # dropped WITH a reason -> no halt
    decisions = json.loads(_state(tmp_path, ART_BULLET_DECISIONS).read_text())
    reporting_id = ids["1.0"]
    assert reporting_id in decisions
    # anchor_reason propagation prefix per the frozen contract
    assert decisions[reporting_id].startswith("anchor_reason:")
    assert "JD is finance-lite" in decisions[reporting_id]


# ===========================================================================
# done_when 3 — no-fabrication HALT (uncovered must-have / unreasoned drop)
# ===========================================================================


def test_fit_score_halts_on_gap_must_have(tmp_path: Path):
    cfg, _ = _setup(tmp_path)
    backend = RoutingBackend({"jd_parsed": _jd_payload(), "fit_score": _fit_payload(level="GAP")})
    result = _run(cfg, tmp_path, backend)
    assert result.status == "halt"
    assert result.halt_node == NODE_FIT_SCORE
    assert "UNCOVERED_MUST_HAVE" in (result.reason or "")
    # short-circuit: bullet-select never ran
    assert NODE_BULLET_SELECT not in result.results
    # halt is never cached -> no fit-score artifact on disk
    assert not _state(tmp_path, ART_FIT_SCORE).exists()


def test_bullet_select_halts_on_unreasoned_anchor_drop(tmp_path: Path):
    cfg, ids = _setup(tmp_path)
    # Drop the $250M money anchor with NO logged reason
    backend = RoutingBackend({"jd_parsed": _jd_payload(), "fit_score": _fit_payload(),
                              "bullet_selection": _bullet_payload(ids, drop_anchor="money")})
    result = _run(cfg, tmp_path, backend)
    assert result.status == "halt"
    assert result.halt_node == NODE_BULLET_SELECT
    assert "ANCHOR_DROP_REQUIRES_INQUIRY" in (result.reason or "")
    # halt is not cached: selection artifact must NOT be present as a final
    assert not _state(tmp_path, ART_BULLET_SELECTION).exists()


def test_bullet_select_halts_on_uncovered_must_have(tmp_path: Path):
    cfg, ids = _setup(tmp_path)
    backend = RoutingBackend({"jd_parsed": _jd_payload(), "fit_score": _fit_payload(),
                              "bullet_selection": _bullet_payload(ids, uncovered=["Kubernetes"])})
    result = _run(cfg, tmp_path, backend)
    assert result.status == "halt"
    assert result.halt_node == NODE_BULLET_SELECT
    assert "UNCOVERED_MUST_HAVE" in (result.reason or "")


def test_bullet_select_halts_on_unaccounted_master_bullet(tmp_path: Path):
    """roborev 1066: a master bullet omitted from the selection must halt, not be
    silently dropped from work.yml."""
    cfg, ids = _setup(tmp_path)
    payload = _bullet_payload(ids)
    # Drop the non-anchor mentor bullet (0.1) entirely from the selection.
    payload["positions"][0]["bullets"] = [payload["positions"][0]["bullets"][0]]
    backend = RoutingBackend({"jd_parsed": _jd_payload(), "fit_score": _fit_payload(),
                              "bullet_selection": payload})
    result = _run(cfg, tmp_path, backend)
    assert result.status == "halt"
    assert result.halt_node == NODE_BULLET_SELECT
    assert "UNACCOUNTED_MASTER_BULLET" in (result.reason or "")
    assert ids["0.1"] in (result.reason or "")  # names the dropped bullet


def test_full_gather_pipeline_runs_to_ok_and_resumes(tmp_path: Path):
    cfg, ids = _setup(tmp_path)
    backend = RoutingBackend({"jd_parsed": _jd_payload(), "fit_score": _fit_payload(),
                              "bullet_selection": _bullet_payload(ids)})
    result = _run(cfg, tmp_path, backend)
    assert result.status == "ok"
    assert set(result.results) == {NODE_JD_PARSE, NODE_FIT_SCORE, NODE_BULLET_SELECT}
    first_calls = len(backend.calls)
    # second run resumes from the bare artifacts -> no new backend calls
    _run(cfg, tmp_path, backend)
    assert len(backend.calls) == first_calls
