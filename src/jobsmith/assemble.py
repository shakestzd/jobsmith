"""Pre-render assembly — read .apply-state/* and write _variables.yml.

The Quarto rendering layer consumes state via the `{{< var >}}` shortcode,
which reads from a `_variables.yml` file alongside the document. This
module is the bridge — it reads the structured artifacts each specialist
wrote into `.apply-state/` and emits a single YAML file the partials
can consume.

Public API:
    assemble_application(slug, applications_dir) -> Path
        Reads private/applications/{slug}/.apply-state/* and writes
        private/applications/{slug}/_variables.yml. Returns the path
        of the written file.

    assemble_all(applications_dir) -> list[Path]
        Iterate over every application under applications_dir and
        assemble each. Used as the Quarto pre-render hook.

The CLI surface is `jobsmith assemble` (per-app) and `jobsmith assemble --all`
(site-wide).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


# ---------- per-source loaders ----------


def _load_jd_parsed(state_dir: Path) -> dict[str, Any]:
    path = state_dir / "jd-parsed.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _load_fit_score(state_dir: Path) -> dict[str, Any]:
    path = state_dir / "fit-score.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _load_bullet_selection(state_dir: Path) -> dict[str, Any]:
    path = state_dir / "bullet-selection.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _load_hm_snippet(state_dir: Path) -> dict[str, Any]:
    """Parse the HM dossier sentinel/markdown into a dict.

    The hm-snippet.md format is:

        # HM dossier (or sentinel)

        detected: yes | no
        name: <string|null>
        source: linkedin_post | jd_signature | shakes_arg | none
        one_specific_signal: <string|null>
        suggested_hook: <string|null>
    """
    path = state_dir / "hm-snippet.md"
    if not path.exists():
        return {}
    out: dict[str, Any] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            value = value.strip()
            if value in ("null", "none", ""):
                out[key.strip()] = None
            elif value in ("yes", "no"):
                out[key.strip()] = value == "yes"
            else:
                out[key.strip()] = value
    return out


def _load_text_artifact(state_dir: Path, filename: str) -> str | None:
    """Load a markdown artifact's full text, or None if missing."""
    path = state_dir / filename
    if not path.exists():
        return None
    return path.read_text()


def _load_application_md(app_dir: Path, filename: str) -> str | None:
    """Load a markdown artifact one level above .apply-state/."""
    path = app_dir / filename
    if not path.exists():
        return None
    return path.read_text()


# ---------- markdown renderers ----------
# These pre-compute markdown for list/table shapes so partials can use
# `{{< var foo_md >}}` without needing Lua filters or Python code blocks.


def _bullet_list(items: list[str]) -> str:
    """Render a list of strings as a markdown bullet list."""
    if not items:
        return "_(none)_"
    return "\n".join(f"- {item}" for item in items)


def _keyword_inline(items: list[str]) -> str:
    """Render a list of strings as comma-separated inline code."""
    if not items:
        return "_(none)_"
    return ", ".join(f"`{k}`" for k in items)


def _must_have_table(rows: list[dict[str, Any]]) -> str:
    """Render the fit-score must_have_table as a markdown table."""
    if not rows:
        return "_(no must-haves recorded)_"
    lines = ["| Requirement | Level | Evidence |", "|---|---|---|"]
    level_emoji = {
        "STRONG": "✅ STRONG",
        "HAVE": "✅ HAVE",
        "PARTIAL": "⚠️ PARTIAL",
        "GAP": "❌ GAP",
        "BLOCKER": "🛑 BLOCKER",
    }
    for row in rows:
        req = (row.get("requirement") or "").replace("|", "\\|").replace("\n", " ")
        level = row.get("level") or ""
        level_label = level_emoji.get(level, level)
        evidence = (row.get("evidence") or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {req} | {level_label} | {evidence} |")
    return "\n".join(lines)


def _hm_dossier_md(hm: dict[str, Any]) -> str:
    """Render the HM dossier as a small status card."""
    if not hm.get("detected"):
        return "_No hiring manager detected — cover letter opens with a generic salutation._"
    lines = [
        f"**HM:** {hm.get('name') or '(unnamed)'}",
        f"**Source:** {hm.get('source') or '(none)'}",
    ]
    if hm.get("one_specific_signal"):
        lines.append(f"**Signal:** {hm['one_specific_signal']}")
    if hm.get("suggested_hook"):
        lines.append(f"**Suggested hook:** {hm['suggested_hook']}")
    return "\n\n".join(lines)


# ---------- public API ----------


def assemble_application(slug: str, applications_dir: Path) -> Path:
    """Read .apply-state/* for one application and write _variables.yml.

    Returns the path of the written file. Raises ValueError if the
    application directory or .apply-state subdir doesn't exist.
    """
    app_dir = applications_dir / slug
    state_dir = app_dir / ".apply-state"
    if not app_dir.is_dir():
        raise ValueError(f"application directory not found: {app_dir}")
    if not state_dir.is_dir():
        raise ValueError(f".apply-state/ not found in {app_dir}")

    jd = _load_jd_parsed(state_dir)
    fit = _load_fit_score(state_dir)
    bullets = _load_bullet_selection(state_dir)
    hm = _load_hm_snippet(state_dir)
    cover_letter = _load_application_md(app_dir, "cover-letter-draft.md")
    company_research = _load_text_artifact(state_dir, "company-research.md")
    outreach = _load_text_artifact(state_dir, "outreach-snippets.md")
    bullet_diff = _load_text_artifact(state_dir, "bullet-diff.md")

    must_haves_list = jd.get("must_haves", []) or []
    nice_to_haves_list = jd.get("nice_to_haves", []) or []
    top_keywords_list = jd.get("top_keywords", []) or []
    must_have_table = fit.get("must_have_table", []) or []
    matched_evidence_list = fit.get("matched_evidence", []) or []
    concerns_list = fit.get("concerns", []) or []
    hm_dict = {
        "detected": hm.get("detected", False),
        "name": hm.get("name"),
        "source": hm.get("source"),
        "one_specific_signal": hm.get("one_specific_signal"),
        "suggested_hook": hm.get("suggested_hook"),
    }

    # Compose the _variables.yml shape.
    variables: dict[str, Any] = {
        "slug": slug,
        "company": jd.get("company"),
        "position": jd.get("position"),
        "location": jd.get("location"),
        "location_type": jd.get("location_type"),
        "salary_range": jd.get("salary_range"),
        "req_id": jd.get("req_id"),
        "apply_url": jd.get("apply_url"),
        "role_type": jd.get("role_type"),
        "jd": {
            "must_haves": must_haves_list,
            "must_haves_md": _bullet_list(must_haves_list),
            "nice_to_haves": nice_to_haves_list,
            "nice_to_haves_md": _bullet_list(nice_to_haves_list),
            "top_keywords": top_keywords_list,
            "top_keywords_md": _keyword_inline(top_keywords_list),
            "text_clean": jd.get("jd_text_clean"),
        },
        "fit": {
            "score": fit.get("score"),
            "score_raw": fit.get("score_raw"),
            "rationale": fit.get("rationale"),
            "specialty": fit.get("specialty"),
            "confidence": fit.get("confidence"),
            "must_have_table": must_have_table,
            "must_have_table_md": _must_have_table(must_have_table),
            "matched_evidence": matched_evidence_list,
            "matched_evidence_md": _bullet_list(matched_evidence_list),
            "concerns": concerns_list,
            "concerns_md": _bullet_list(concerns_list),
            "pitch": fit.get("pitch"),
        },
        "bullets": {
            "positions": bullets.get("positions", []),
            "anchor_bullets_master": bullets.get("anchor_bullets_master", []),
            "anchor_bullets_kept": bullets.get("anchor_bullets_kept", []),
            "anchor_bullets_dropped": bullets.get("anchor_bullets_dropped", []),
        },
        "hm": hm_dict,
        "hm_md": _hm_dossier_md(hm_dict),
        "cover_letter_draft": cover_letter,
        "company_research": company_research,
        "outreach": outreach,
        "bullet_diff": bullet_diff,
    }

    out_path = app_dir / "_variables.yml"
    out_path.write_text(yaml.safe_dump(variables, sort_keys=False, allow_unicode=True))
    return out_path


def assemble_all(applications_dir: Path) -> list[Path]:
    """Assemble every application directory under `applications_dir`."""
    if not applications_dir.is_dir():
        raise ValueError(f"applications directory not found: {applications_dir}")

    written: list[Path] = []
    for entry in sorted(applications_dir.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("_") or entry.name.startswith("."):
            continue
        if not (entry / ".apply-state").is_dir():
            continue
        try:
            written.append(assemble_application(entry.name, applications_dir))
        except ValueError:
            # Skip malformed entries silently — they were already filtered above
            continue
    return written


__all__ = ["assemble_all", "assemble_application"]
