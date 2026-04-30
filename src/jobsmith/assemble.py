"""Pre-render assembly — read .apply-state/* and emit a self-contained Quarto project.

The Quarto rendering layer reads scalars via `{{< var >}}` (from `_variables.yml`)
and block content via `{{< include >}}` (from `_blocks/*.md`). To make those
shortcodes resolve correctly regardless of where the workflow is rendered from,
this module makes each application directory a **self-contained Quarto project**:

    <app>/
    ├── _quarto.yml             ← makes it a project; pre-render hook + format
    ├── _variables.yml          ← scalars (company, position, score, etc.)
    ├── _blocks/                ← block-level markdown (lists, tables, fallbacks)
    ├── _partials/              ← symlink to jobsmith templates/partials/
    ├── workflow.qmd            ← copied from templates/workflow/_workflow.qmd
    ├── .apply-state/           ← unchanged — source of truth for specialists
    ├── documents/              ← resume.pdf + cover-letter.pdf live here
    └── cover-letter-draft.md

Partials and the workflow QMD use project-root-absolute paths (e.g.
`{{< include /_blocks/foo.md >}}`) so they resolve correctly regardless
of include depth.

Public API:
    assemble_application(slug, applications_dir, partials_src=None,
                         workflow_src=None) -> Path
        Reads <app>/.apply-state/* and writes <app>/_variables.yml,
        <app>/_blocks/*.md, <app>/_quarto.yml, and the _partials symlink
        + workflow.qmd copy. Returns the path of _variables.yml.

    assemble_all(applications_dir, ...) -> list[Path]
        Iterate over every application directory and assemble each.
        Used as the Quarto pre-render hook.

The CLI surface is `jobsmith assemble` (per-app) and `jobsmith assemble --all`
(site-wide).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

from .config import load_config

# Default location of the bundled partials + workflow templates within jobsmith.
# Resolved as <package_root>/templates/... assuming the package is installed
# from a checkout (uv pip install -e .) or vendored as a Claude Code plugin.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PARTIALS_SRC = PACKAGE_ROOT / "templates" / "partials"
DEFAULT_WORKFLOW_SRC = PACKAGE_ROOT / "templates" / "workflow" / "_workflow.qmd"


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


def _resume_preview_md(
    resume_pdf_exists: bool,
    resume_qmd_exists: bool,
) -> str:
    """Render the resume preview block — conditional on file existence."""
    if not (resume_pdf_exists or resume_qmd_exists):
        return (
            "::: {.callout-warning appearance=\"minimal\"}\n"
            "**Resume not yet rendered.** Run `apply-resume-renderer` "
            "(or `quarto render documents/resume.qmd`) to produce "
            "`documents/resume.pdf`, then re-run "
            "`jobsmith assemble {slug}` to populate this section.\n"
            ":::\n"
        )
    lines = ["::: {.callout-note appearance=\"simple\"}"]
    if resume_pdf_exists:
        lines.append("**File:** [`documents/resume.pdf`](documents/resume.pdf)\\")
    if resume_qmd_exists:
        lines.append("**Source:** [`documents/resume.qmd`](documents/resume.qmd)")
    lines.append(":::")
    if resume_pdf_exists:
        lines.append("")
        lines.append("```{=html}")
        lines.append(
            '<embed src="documents/resume.pdf" type="application/pdf" '
            'width="100%" height="800" />'
        )
        lines.append("```")
    return "\n".join(lines) + "\n"


def _quarto_project_yml(slug: str) -> str:
    """Minimal _quarto.yml that makes the application directory a project."""
    return (
        "# Auto-generated by `jobsmith assemble`. Do not edit; re-run assemble.\n"
        f"project:\n"
        f"  type: default\n"
        f"  title: \"{slug}\"\n"
        f"\n"
        f"format:\n"
        f"  html:\n"
        f"    toc: true\n"
        f"    toc-depth: 3\n"
        f"    toc-location: left\n"
        f"    embed-resources: true\n"
        f"    page-layout: full\n"
    )


# ---------- public API ----------


def assemble_application(
    slug: str,
    applications_dir: Path,
    partials_src: Path | None = None,
    workflow_src: Path | None = None,
) -> Path:
    """Read .apply-state/* and assemble a self-contained Quarto project.

    Writes:
      <app>/_variables.yml       (scalars for {{< var >}})
      <app>/_blocks/*.md         (block-level markdown for {{< include >}})
      <app>/_quarto.yml          (makes it a project; partials resolve correctly)
      <app>/_partials            (symlink to jobsmith templates/partials)
      <app>/workflow.qmd         (copy of templates/workflow/_workflow.qmd)

    Returns the path of _variables.yml. Raises ValueError if the
    application directory or .apply-state subdir doesn't exist.
    """
    app_dir = applications_dir / slug
    state_dir = app_dir / ".apply-state"
    if not app_dir.is_dir():
        raise ValueError(f"application directory not found: {app_dir}")
    if not state_dir.is_dir():
        raise ValueError(f".apply-state/ not found in {app_dir}")

    partials_src = partials_src or DEFAULT_PARTIALS_SRC
    workflow_src = workflow_src or DEFAULT_WORKFLOW_SRC

    jd = _load_jd_parsed(state_dir)
    fit = _load_fit_score(state_dir)
    bullets = _load_bullet_selection(state_dir)
    hm = _load_hm_snippet(state_dir)
    cover_letter = _load_application_md(app_dir, "cover-letter-draft.md")
    company_research = _load_text_artifact(state_dir, "company-research.md")
    outreach = _load_text_artifact(state_dir, "outreach-snippets.md")
    bullet_diff = _load_text_artifact(state_dir, "bullet-diff.md")

    # Resolve artifact paths (relative to the application dir for portability)
    resume_pdf_path = app_dir / "documents" / "resume.pdf"
    cover_letter_pdf_path = app_dir / "documents" / "cover-letter.pdf"
    resume_qmd_path = app_dir / "documents" / "resume.qmd"

    # Pull user identity from .apply-config.yaml (walks up from app_dir).
    # Returns defaults (empty strings) if no config found, which is fine —
    # the workflow QMD will render with empty author and the user can edit
    # their config to fix it.
    config = load_config(search_from=app_dir)
    user_identity = {
        "name": config.user.name,
        "email": config.user.email,
        "phone": config.user.phone,
        "location": config.user.location,
        "github": config.user.github,
        "linkedin": config.user.linkedin,
    }

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
        "user": user_identity,
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
        "artifacts": {
            "resume_pdf": "documents/resume.pdf" if resume_pdf_path.exists() else None,
            "resume_qmd": "documents/resume.qmd" if resume_qmd_path.exists() else None,
            "cover_letter_pdf": (
                "documents/cover-letter.pdf" if cover_letter_pdf_path.exists() else None
            ),
            "cover_letter_md": (
                "cover-letter-draft.md" if cover_letter is not None else None
            ),
        },
    }

    out_path = app_dir / "_variables.yml"
    out_path.write_text(yaml.safe_dump(variables, sort_keys=False, allow_unicode=True))

    # Markdown block-level content (lists, tables) MUST be written to separate
    # files because Quarto's {{< var >}} shortcode inlines values as text and
    # smushes block structure. Partials pull these in via {{< include >}}, which
    # preserves markdown blocks.
    blocks_dir = app_dir / "_blocks"
    blocks_dir.mkdir(exist_ok=True)
    (blocks_dir / "must-haves.md").write_text(_bullet_list(must_haves_list) + "\n")
    (blocks_dir / "nice-to-haves.md").write_text(_bullet_list(nice_to_haves_list) + "\n")
    (blocks_dir / "top-keywords.md").write_text(_keyword_inline(top_keywords_list) + "\n")
    (blocks_dir / "must-have-table.md").write_text(_must_have_table(must_have_table) + "\n")
    (blocks_dir / "matched-evidence.md").write_text(_bullet_list(matched_evidence_list) + "\n")
    (blocks_dir / "concerns.md").write_text(_bullet_list(concerns_list) + "\n")
    (blocks_dir / "hm-dossier.md").write_text(_hm_dossier_md(hm_dict) + "\n")
    (blocks_dir / "cover-letter.md").write_text(
        (cover_letter or "_(no cover letter draft)_") + "\n"
    )
    (blocks_dir / "resume-preview.md").write_text(
        _resume_preview_md(
            resume_pdf_path.exists(),
            resume_qmd_path.exists(),
        )
    )

    # Make the application directory a self-contained Quarto project so
    # project-root-absolute include paths (`/_blocks/foo.md`,
    # `/_partials/foo.qmd`) resolve correctly.
    quarto_yml = app_dir / "_quarto.yml"
    if not quarto_yml.exists():
        quarto_yml.write_text(_quarto_project_yml(slug))

    # Symlink the partials directory from the jobsmith templates so each app
    # picks up partial updates automatically. If a symlink isn't possible
    # (e.g., Windows without dev-mode), fall back to a directory copy.
    partials_link = app_dir / "_partials"
    if partials_src.is_dir():
        if partials_link.is_symlink() or partials_link.exists():
            try:
                if partials_link.is_symlink():
                    partials_link.unlink()
                else:
                    # Best-effort cleanup of a stale copy
                    import shutil
                    shutil.rmtree(partials_link)
            except OSError:
                pass
        try:
            os.symlink(partials_src, partials_link, target_is_directory=True)
        except (OSError, NotImplementedError):
            # Fallback: copy the directory contents
            import shutil
            shutil.copytree(partials_src, partials_link)

    # Copy the workflow QMD into the application root so the user can edit
    # the per-application review surface without modifying the template.
    if workflow_src.is_file():
        target_workflow = app_dir / "workflow.qmd"
        if not target_workflow.exists():
            target_workflow.write_text(workflow_src.read_text())

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
