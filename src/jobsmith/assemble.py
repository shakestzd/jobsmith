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
    ├── index.qmd               ← copied from templates/workflow/_index.qmd
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
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from .config import load_config

# Default location of the bundled partials + workflow templates within jobsmith.
# Resolved as <package_root>/templates/... assuming the package is installed
# from a checkout (uv pip install -e .) or vendored as a Claude Code plugin.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PARTIALS_SRC = PACKAGE_ROOT / "templates" / "partials"
DEFAULT_INDEX_SRC = PACKAGE_ROOT / "templates" / "workflow" / "_index.qmd"
# Backwards-compat alias for callers still using `workflow_src=`. Removed in
# a future release — the per-app file is now `index.qmd` to match Quarto's
# website-page convention.
DEFAULT_WORKFLOW_SRC = DEFAULT_INDEX_SRC


# ---------- theme helpers ----------


def _slugify_company(company: str) -> str:
    """Convert a company name to a filesystem-safe slug.

    Rules:
      - Lowercase
      - Spaces and underscores → hyphens
      - Strip any character not in [a-z0-9-]
      - Collapse multiple consecutive hyphens to one
      - Strip leading/trailing hyphens

    Examples:
      "Schneider Electric" → "schneider-electric"
      "PwC"               → "pwc"
      "Microsoft Corp."   → "microsoft-corp"
    """
    slug = company.lower()
    slug = re.sub(r"[ _]+", "-", slug)
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    return slug.strip("-")


def _resolve_theme(
    slug: str,
    company: str | None,
    app_dir: Path,
    package_root: Path,
) -> Path:
    """Walk the theme resolution chain and return the winning SCSS path.

    Priority (high → low):
      1. <app_dir>/theme.scss   — user's per-app override (already present)
      2. <package_root>/templates/themes/companies/<slug>.scss — curated
      3. <package_root>/templates/themes/default.scss          — fallback

    The ``slug`` parameter is the pre-computed company slug (already slugified).
    If ``company`` is provided and ``slug`` does not match any curated file,
    the function also tries slugifying ``company`` directly (robustness).
    """
    # 1. User override — if theme.scss is already a real file, leave it alone.
    app_theme = app_dir / "theme.scss"
    if app_theme.exists() and not app_theme.is_symlink():
        return app_theme
    if app_theme.is_symlink() and app_theme.resolve().exists():
        return app_theme

    themes_root = package_root / "templates" / "themes"
    companies_dir = themes_root / "companies"

    # 2. Curated company SCSS — try the given slug first, then re-slugify company.
    candidates = [slug]
    if company:
        derived = _slugify_company(company)
        if derived != slug:
            candidates.append(derived)
    for candidate in candidates:
        curated = companies_dir / f"{candidate}.scss"
        if curated.exists():
            return curated

    # 3. Default fallback.
    return themes_root / "default.scss"


def _install_theme(resolved: Path, app_dir: Path) -> None:
    """Symlink (or copy) the resolved SCSS into <app_dir>/theme.scss.

    If the destination already exists and is the correct target, it is left
    untouched. A stale symlink or a copy from a previous run is replaced.
    """
    dest = app_dir / "theme.scss"

    # Already a real file (user override) — never overwrite.
    if dest.exists() and not dest.is_symlink():
        return

    # Remove stale symlink if target has changed.
    if dest.is_symlink():
        if dest.resolve() == resolved.resolve():
            return  # already correct
        dest.unlink()

    try:
        os.symlink(resolved, dest)
    except OSError:
        # Fallback on platforms where symlinks are unavailable (e.g. Windows).
        shutil.copy2(resolved, dest)


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


def _load_ai_tell_report(state_dir: Path) -> dict[str, Any] | None:
    """Load .apply-state/ai-tell-report.json, returning None if missing or malformed.

    Degrades gracefully — callers must not raise on None; use the fallback
    callout block instead.
    """
    import logging

    path = state_dir / "ai-tell-report.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logging.getLogger(__name__).warning(
            "ai-tell-report.json could not be loaded (%s): %s", path, exc
        )
        return None


_HUMANIZER_AUDIT_FALLBACK = (
    '::: {.callout-warning appearance="minimal"}\n'
    "**Awaiting specialist** — `apply-prose-qa` has not yet written "
    "`ai-tell-report.json`. Run the specialist (or `jobsmith assemble {slug}`) "
    "after it completes to populate the humanizer audit section.\n"
    ":::\n"
)


def _render_humanizer_audit_block(report: dict[str, Any] | None) -> str:
    """Format the 6.2 audit + 6.3 final diff from an ai-tell-report into markdown.

    Iterations are sorted by id (6.1 -> 6.2 -> 6.3) regardless of JSON order.
    Returns a fallback awaiting-specialist callout when report is None or
    contains no 6.2/6.3 entries.
    """
    if report is None:
        return _HUMANIZER_AUDIT_FALLBACK

    iterations: list[dict[str, Any]] = sorted(
        report.get("iterations") or [],
        key=lambda it: it.get("id", ""),
    )

    sections: list[str] = []
    for it in iterations:
        it_id = it.get("id", "")
        label = it.get("label", "")

        if it_id == "6.2":
            header = f"### {it_id} Audit --- {label}"
            remaining = it.get("remaining_tells") or []
            verdict = it.get("verdict", "")
            lines = [header, ""]
            if remaining:
                lines.append(f"**Verdict:** {verdict}")
                lines.append("")
                lines.append("| Phrase | Rationale | Severity |")
                lines.append("|---|---|---|")
                for tell in remaining:
                    phrase = (tell.get("phrase") or "").replace("|", r"\|")
                    rationale = (tell.get("rationale") or "").replace("|", r"\|")
                    severity = (tell.get("severity") or "").replace("|", r"\|")
                    lines.append(f"| `{phrase}` | {rationale} | {severity} |")
            else:
                lines.append(f"**Verdict:** {verdict or 'clean'} --- no remaining tells.")
            sections.append("\n".join(lines))

        elif it_id == "6.3":
            header = f"### {it_id} Final --- {label}"
            applied = it.get("applied_fixes") or []
            final_diff = (it.get("final_diff") or "").strip()
            lines = [header, ""]
            if applied:
                lines.append("**Applied fixes:**")
                lines.append("")
                for fix in applied:
                    phrase = fix.get("phrase") or ""
                    replaced = fix.get("replaced_with") or ""
                    lines.append(f"- `{phrase}` -> `{replaced}`")
                lines.append("")
            if final_diff:
                lines.append("**Diff:**")
                lines.append("")
                lines.append("```diff")
                lines.append(final_diff)
                lines.append("```")
            sections.append("\n".join(lines))

    if not sections:
        return _HUMANIZER_AUDIT_FALLBACK

    return "\n\n".join(sections) + "\n"


_COMPANY_RESEARCH_FALLBACK = (
    '::: {.callout-warning appearance="minimal"}\n'
    "**Awaiting specialist** — `apply-company-research` has not yet written "
    "`company-research.md`. Run the specialist (or `jobsmith assemble {slug}`) "
    "after it completes to populate the §4 \"Why work here\" section.\n"
    ":::\n"
)


def _company_research_block(content: str | None) -> str:
    """Return the company-research markdown verbatim or a fallback callout."""
    if content is None or not content.strip():
        return _COMPANY_RESEARCH_FALLBACK
    return content if content.endswith("\n") else content + "\n"


def _load_user_identity(
    app_dir: Path,
    master_author_yml: Path | None,
    config_user: dict[str, str],
) -> dict[str, str]:
    """Resolve user identity by checking, in order:

    1. <app>/documents/author.yml  — per-application author block
    2. <master_author_yml>          — master author.yml (config.master.author_yml)
    3. config.user                  — .apply-config.yaml user fields

    Returns a dict with keys name, email, phone, location, github, linkedin.
    Empty strings for any field not found.
    """
    sources: list[Path] = []
    app_author = app_dir / "documents" / "author.yml"
    if app_author.exists():
        sources.append(app_author)
    if master_author_yml and master_author_yml.exists():
        sources.append(master_author_yml)

    user: dict[str, str] = {
        "name": config_user.get("name", "") or "",
        "email": config_user.get("email", "") or "",
        "phone": config_user.get("phone", "") or "",
        "location": config_user.get("location", "") or "",
        "github": config_user.get("github", "") or "",
        "linkedin": config_user.get("linkedin", "") or "",
    }

    for src in sources:
        try:
            data = yaml.safe_load(src.read_text())
        except (OSError, yaml.YAMLError):
            continue
        if not data:
            continue

        author = data.get("author") if isinstance(data, dict) else None
        if isinstance(author, list) and author:
            author = author[0]
        if not isinstance(author, dict):
            continue

        # Name extraction — handle both flat (firstname/lastname) and
        # nested (name.first/name.last) forms.
        if not user["name"]:
            user["name"] = _extract_author_name(author)

        # Pull from contacts list (icon-tagged structure used by both
        # shakestzd and the Pat Doe example).
        for contact in author.get("contacts", []) or []:
            icon = (contact.get("icon") or "").lower()
            text = (contact.get("text") or "").strip()
            url = (contact.get("url") or "").strip()
            if not user["email"] and ("envelope" in icon or url.startswith("mailto:")):
                user["email"] = text or url.removeprefix("mailto:")
            elif not user["phone"] and ("phone" in icon or url.startswith("tel:")):
                user["phone"] = text or url.removeprefix("tel:")
            elif not user["location"] and "location" in icon:
                user["location"] = text
            elif not user["github"] and "github" in icon:
                user["github"] = text or url.rsplit("/", 1)[-1]
            elif not user["linkedin"] and "linkedin" in icon:
                user["linkedin"] = text or url.rsplit("/", 1)[-1]

        # Top-level email/phone fallback (Pat Doe-style schema).
        if not user["email"] and isinstance(author.get("email"), str):
            user["email"] = author["email"]
        if not user["phone"] and isinstance(author.get("phone"), str):
            user["phone"] = author["phone"]
        if not user["location"] and isinstance(author.get("address"), str):
            user["location"] = author["address"]

    return user


def _extract_letter_body(letter: str | None) -> str:
    """Extract the body-only portion of a cover letter for portal paste.

    Strips the contact-info header (name + contact lines), the date line,
    and the addressee block. The resulting text starts at the salutation
    ('Dear ...,' / 'Hello,' etc.) and includes the closing.

    Used by the Step 7 copy-paste partial. Falls back to the full letter
    if structural markers can't be found.
    """
    if not letter:
        return "_(no cover letter draft)_"

    lines = letter.splitlines()
    # Find the first salutation line. Common forms: 'Dear X,', 'Hello,'.
    salutation_idx = next(
        (i for i, line in enumerate(lines) if line.lstrip().startswith(("Dear ", "Hello,", "Hi "))),
        None,
    )
    if salutation_idx is None:
        return letter
    return "\n".join(lines[salutation_idx:])


def _extract_author_name(author: dict[str, Any]) -> str:
    """Build a display name from an author dict.

    Handles both the shakestzd shape (firstname/lastname flat) and the
    Pat Doe shape (nested name.first / name.middle / name.last).
    """
    if "firstname" in author or "lastname" in author:
        first = (author.get("firstname") or "").strip()
        last = (author.get("lastname") or "").strip()
        return f"{first} {last}".strip()
    name = author.get("name")
    if isinstance(name, dict):
        parts = [
            (name.get("first") or "").strip(),
            (name.get("middle") or "").strip(),
            (name.get("last") or "").strip(),
        ]
        return " ".join(p for p in parts if p)
    if isinstance(name, str):
        return name.strip()
    return ""


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


def _outreach_snippets_block(content: str | None) -> str:
    """Return the outreach-snippets block content or a fallback callout.

    When content is provided (the specialist has written outreach-snippets.md),
    return it verbatim — the specialist owns character-count constraints.

    When content is None (specialist hasn't run yet or no HM was detected on a
    portal-only application), return a standard "awaiting specialist" callout
    that the _outreach.qmd partial renders gracefully.
    """
    if content is not None:
        return content
    return (
        "::: {.callout-warning appearance=\"minimal\"}\n"
        "**Awaiting specialist** — `apply-hm-enricher` has not yet written "
        "`outreach-snippets.md`. Run the specialist (or `jobsmith assemble {slug}`) "
        "after it completes to populate this section.\n\n"
        "When no hiring manager is named, this section will contain: "
        "_no HM detected — portal-only application_.\n"
        ":::\n"
    )


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


def _pdf_preview_md(
    pdf_relpath: str | None,
    source_relpath: str | None,
    not_rendered_message: str,
) -> str:
    """Render a generic PDF preview block — link + embed when present, fallback when not.

    Used for both the resume PDF and the cover letter PDF.
    """
    if not (pdf_relpath or source_relpath):
        return (
            "::: {.callout-warning appearance=\"minimal\"}\n"
            f"{not_rendered_message}\n"
            ":::\n"
        )
    lines = ["::: {.callout-note appearance=\"simple\"}"]
    if pdf_relpath:
        lines.append(f"**File:** [`{pdf_relpath}`]({pdf_relpath})\\")
    if source_relpath:
        lines.append(f"**Source:** [`{source_relpath}`]({source_relpath})")
    lines.append(":::")
    if pdf_relpath:
        lines.append("")
        lines.append("```{=html}")
        lines.append(
            f'<embed src="{pdf_relpath}" type="application/pdf" '
            'width="100%" height="800" />'
        )
        lines.append("```")
    return "\n".join(lines) + "\n"


def _resume_preview_md(
    resume_pdf_exists: bool,
    resume_qmd_exists: bool,
) -> str:
    """Render the resume preview block — conditional on file existence."""
    return _pdf_preview_md(
        pdf_relpath="documents/resume.pdf" if resume_pdf_exists else None,
        source_relpath="documents/resume.qmd" if resume_qmd_exists else None,
        not_rendered_message=(
            "**Resume not yet rendered.** Run `apply-resume-renderer` "
            "(or `quarto render documents/resume.qmd`) to produce "
            "`documents/resume.pdf`, then re-run "
            "`jobsmith assemble {slug}` to populate this section."
        ),
    )


def _cover_letter_pdf_md(
    pdf_relpath: str | None,
    qmd_relpath: str | None,
) -> str:
    """Render the cover letter PDF preview block — for upload-type portals."""
    return _pdf_preview_md(
        pdf_relpath=pdf_relpath,
        source_relpath=qmd_relpath,
        not_rendered_message=(
            "**Cover letter PDF not yet rendered.** Run "
            "`quarto render cover-letter.qmd` (or the equivalent for your "
            "template path) to produce a PDF, then re-run "
            "`jobsmith assemble {slug}` to populate this section. "
            "The plaintext copy-paste version above remains usable for "
            "single-text-field portals while the PDF is pending."
        ),
    )


def _quarto_project_yml(slug: str) -> str:
    """Minimal _quarto.yml that makes the application directory a project.

    Always includes ``format.html.theme: [cosmo, theme.scss]`` so the per-app
    theme file (resolved and installed by ``_resolve_theme`` / ``_install_theme``)
    is picked up on every render.
    """
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
        f"    theme: [cosmo, theme.scss]\n"
    )


# ---------- public API ----------


def assemble_application(
    slug: str,
    applications_dir: Path,
    partials_src: Path | None = None,
    workflow_src: Path | None = None,
    package_root: Path | None = None,
) -> Path:
    """Read .apply-state/* and assemble a self-contained Quarto project.

    Writes:
      <app>/_variables.yml       (scalars for {{< var >}})
      <app>/_blocks/*.md         (block-level markdown for {{< include >}})
      <app>/_quarto.yml          (makes it a project; partials resolve correctly)
      <app>/_partials            (symlink to jobsmith templates/partials)
      <app>/index.qmd            (copy of templates/workflow/_index.qmd)
      <app>/theme.scss           (symlink/copy of resolved per-company theme)

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
    pkg_root = package_root or PACKAGE_ROOT

    jd = _load_jd_parsed(state_dir)
    fit = _load_fit_score(state_dir)
    bullets = _load_bullet_selection(state_dir)
    hm = _load_hm_snippet(state_dir)
    cover_letter = _load_application_md(app_dir, "cover-letter-draft.md")
    company_research = _load_text_artifact(state_dir, "company-research.md")
    outreach = _load_text_artifact(state_dir, "outreach-snippets.md")
    bullet_diff = _load_text_artifact(state_dir, "bullet-diff.md")
    ai_tell_report = _load_ai_tell_report(state_dir)

    # Resolve artifact paths (relative to the application dir for portability).
    # Cover letter PDF can live at the app root or under documents/ depending
    # on which template the user picked.
    resume_pdf_path = app_dir / "documents" / "resume.pdf"
    resume_qmd_path = app_dir / "documents" / "resume.qmd"
    cover_letter_pdf_root = app_dir / "cover-letter.pdf"
    cover_letter_pdf_documents = app_dir / "documents" / "cover-letter.pdf"
    cover_letter_qmd_root = app_dir / "cover-letter.qmd"
    if cover_letter_pdf_root.exists():
        cover_letter_pdf_path = cover_letter_pdf_root
        cover_letter_pdf_relpath = "cover-letter.pdf"
    elif cover_letter_pdf_documents.exists():
        cover_letter_pdf_path = cover_letter_pdf_documents
        cover_letter_pdf_relpath = "documents/cover-letter.pdf"
    else:
        cover_letter_pdf_path = None
        cover_letter_pdf_relpath = None

    # Resolve user identity by checking, in order:
    #   1. <app>/documents/author.yml   — per-application author block
    #   2. master.author_yml            — from .apply-config.yaml
    #   3. .apply-config.yaml user      — fallback config-level
    #   4. empty defaults
    # This means jobsmith works against existing repos that have an
    # author.yml without requiring the user to maintain a separate
    # .apply-config.yaml user section.
    config = load_config(search_from=app_dir)
    config_user_dict = {
        "name": config.user.name,
        "email": config.user.email,
        "phone": config.user.phone,
        "location": config.user.location,
        "github": config.user.github,
        "linkedin": config.user.linkedin,
    }
    repo_root = app_dir.parent.parent  # private/applications/<slug>/.. /..
    master_author = (
        config.master.author_yml
        if config.master.author_yml.is_absolute()
        else repo_root / config.master.author_yml
    )
    user_identity = _load_user_identity(app_dir, master_author, config_user_dict)

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
            "cover_letter_pdf": cover_letter_pdf_relpath,
            "cover_letter_qmd": (
                "cover-letter.qmd" if cover_letter_qmd_root.exists() else None
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
    # Body-only variant for portal paste — strips the contact-info header
    # and the date line above the addressee block. Useful for portals with
    # a single text field for the cover letter.
    (blocks_dir / "cover-letter-body.md").write_text(
        _extract_letter_body(cover_letter) + "\n"
    )
    (blocks_dir / "resume-preview.md").write_text(
        _resume_preview_md(
            resume_pdf_path.exists(),
            resume_qmd_path.exists(),
        )
    )
    (blocks_dir / "cover-letter-pdf.md").write_text(
        _cover_letter_pdf_md(
            pdf_relpath=cover_letter_pdf_relpath,
            qmd_relpath=(
                "cover-letter.qmd" if cover_letter_qmd_root.exists() else None
            ),
        )
    )
    (blocks_dir / "outreach-snippets.md").write_text(
        _outreach_snippets_block(outreach)
    )
    (blocks_dir / "humanizer-audit.md").write_text(
        _render_humanizer_audit_block(ai_tell_report)
    )
    (blocks_dir / "company-research.md").write_text(
        _company_research_block(company_research)
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

    # Copy the per-app index QMD into the application root so the user can
    # edit the review surface without modifying the template. The file is
    # named index.qmd to match Quarto's website-page convention; the site-level
    # listings page (templates/site/index.qmd) reads from each app's index.qmd
    # via Quarto listings.
    if workflow_src.is_file():
        target_index = app_dir / "index.qmd"
        if not target_index.exists():
            target_index.write_text(workflow_src.read_text())
        # One-time migration: rename any legacy workflow.qmd that pre-dates
        # this convention. Idempotent: no-op once the rename has happened.
        legacy = app_dir / "workflow.qmd"
        if legacy.exists() and legacy.resolve() != target_index.resolve():
            legacy.unlink()

    # Resolve and install the per-company theme SCSS.
    # The company name comes from jd-parsed.json; fall back to the slug.
    company_name = jd.get("company") or ""
    company_slug = _slugify_company(company_name) if company_name else slug
    resolved_theme = _resolve_theme(
        slug=company_slug,
        company=company_name or None,
        app_dir=app_dir,
        package_root=pkg_root,
    )
    _install_theme(resolved_theme, app_dir)

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


__all__ = [
    "assemble_all",
    "assemble_application",
    "_company_research_block",
    "_load_ai_tell_report",
    "_outreach_snippets_block",
    "_render_humanizer_audit_block",
    "_resolve_theme",
    "_slugify_company",
]
