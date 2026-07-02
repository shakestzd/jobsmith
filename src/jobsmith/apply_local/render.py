"""Local (offline) resume render + portfolio assembly for the code_local apply
(feat-d1ef000b, roborev 1061 finding 1).

After gather->draft produce the tailored documents + ``.apply-state`` artifacts,
this module finishes the LOCAL apply PURELY in code (no ``claude -p`` / headless
/ specialist agents):

* build ``documents/resume.qmd`` from the prose draft's Professional Summary plus
  the master author/education — mirroring the ``awesomecv-typst`` template the
  cloud path emits (Professional Summary prose + the three ``{{< yaml >}}``
  shortcodes for work/education/skill + the author title);
* render it to ``documents/resume.pdf`` via ``quarto`` when quarto is installed
  (gracefully SKIPPED — never a FAKE PDF — when it is not);
* assemble the self-contained Quarto portfolio project (best-effort).

The model-driven polish (ATS check, cover letter, index frontmatter) stays OUT
of the local path. Every failure is captured into a :class:`RenderResult` —
:func:`render_local` NEVER raises, so a render problem can never lose the
gather/draft artifacts.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from jobsmith.apply_local.checkpoint import apply_state_dir
from jobsmith.apply_local.nodes_draft import ART_PROSE_DRAFT, run_prose_qa_checks
from jobsmith.assemble import PACKAGE_ROOT, assemble_application
from jobsmith.config import JobsmithConfig
from jobsmith.paths import resolve

logger = logging.getLogger(__name__)

RENDER_TIMEOUT_S = 300
RESUME_QMD = "resume.qmd"
RESUME_PDF = "resume.pdf"
_PROF_SUMMARY = "Professional Summary"
# Gather writes these tailored files into documents/; they MUST exist to render.
_GATHER_DOCS = ("work.yml", "skill.yml")


@dataclass
class RenderResult:
    """Outcome of the local render: ``status`` is ``ok`` | ``skipped`` | ``error``.

    ``skipped`` means quarto is not installed (the .qmd is still built — no fake
    PDF). ``error`` carries a ``reason`` (a stderr tail or a clear precondition
    message). ``artifacts`` collects the on-disk paths produced.
    """

    status: str
    pdf_path: str | None = None
    reason: str | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    # The resume renders its work bullets from work.yml, NOT prose-draft.md — so
    # the prose-qa gate does not cover them. We re-run the deterministic checks on
    # the ACTUAL resume bullets so un-QA'd bullet text never silently ships
    # (roborev 1066). ``qa_pass`` False means blocking findings on those bullets.
    qa_pass: bool = True
    qa_findings: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# resume.qmd construction
# ---------------------------------------------------------------------------


def _author_name(author_yml: Path) -> str:
    """Display name from a documents/author.yml (flat or nested ``name`` shapes)."""
    if not author_yml.is_file():
        return ""
    try:
        data = yaml.safe_load(author_yml.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return ""
    author = data.get("author") if isinstance(data, dict) else None
    if isinstance(author, list) and author:
        author = author[0]
    if not isinstance(author, dict):
        return ""
    if author.get("firstname") or author.get("lastname"):
        first = str(author.get("firstname", "") or "").strip()
        last = str(author.get("lastname", "") or "").strip()
        return f"{first} {last}".strip()
    name = author.get("name")
    if isinstance(name, dict):
        parts = [str(name.get(k, "") or "").strip() for k in ("first", "middle", "last")]
        return " ".join(p for p in parts if p)
    return name.strip() if isinstance(name, str) else ""


def _extract_professional_summary(markdown: str) -> str:
    """Return the prose under the 'Professional Summary' heading of a draft.

    Robust to the heading level (the local writer emits ``#`` or ``##``):
    collects every line after that heading up to the next markdown heading.
    Falls back to the draft's leading prose (text before the first heading) when
    no Professional-Summary heading is present.
    """
    lines = markdown.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") and _PROF_SUMMARY.lower() in stripped.lower():
            start = i + 1
            break
    if start is None:
        return _leading_prose(lines)
    body: list[str] = []
    for line in lines[start:]:
        if line.lstrip().startswith("#"):
            break
        body.append(line)
    return "\n".join(body).strip()


def _leading_prose(lines: list[str]) -> str:
    """Prose before the first heading (fallback when no summary heading exists)."""
    body: list[str] = []
    for line in lines:
        if line.lstrip().startswith("#"):
            if body:
                break
            continue
        body.append(line)
    return "\n".join(body).strip()


def _resume_qmd(name: str, summary: str) -> str:
    """The minimal-but-valid awesomecv-typst resume.qmd we emit (mirrors cloud)."""
    title = name or "Resume"
    return (
        "---\n"
        f'title: "{title}"\n'
        "metadata-files:\n"
        "  - author.yml\n"
        "format:\n"
        "  awesomecv-typst: default\n"
        "keep-typ: true\n"
        "---\n\n"
        f"## {_PROF_SUMMARY}\n\n"
        f"{summary}\n\n"
        "## Work Experience\n\n"
        "{{< yaml work.yml >}}\n\n"
        "## Education\n\n"
        "{{< yaml education.yml >}}\n\n"
        "## Skills\n\n"
        "{{< yaml skill.yml >}}\n"
    )


# ---------------------------------------------------------------------------
# Filesystem preparation
# ---------------------------------------------------------------------------


def _ensure_documents(documents: Path, config: JobsmithConfig, repo_root: Path) -> str | None:
    """Validate gather output and copy the master author/education into documents/.

    Returns a clear error string when a gather-owned file is missing; else None.
    """
    for required in _GATHER_DOCS:
        if not (documents / required).is_file():
            return f"gather artifact missing: documents/{required} (run gather first)"
    for fname, cfg_path in (
        ("education.yml", config.master.education_yml),
        ("author.yml", config.master.author_yml),
    ):
        dest = documents / fname
        if dest.exists():
            continue
        src = resolve(cfg_path, repo_root)
        if src.is_file():
            shutil.copy2(src, dest)
    return None


def _extensions_src(repo_root: Path) -> Path | None:
    """Locate the bundled awesomecv ``_extensions`` tree quarto needs to render."""
    for cand in (
        repo_root / "shared" / "extensions" / "_extensions",
        repo_root / "templates" / "extensions" / "_extensions",
        PACKAGE_ROOT / "templates" / "extensions" / "_extensions",
    ):
        if cand.is_dir():
            return cand
    return None


def _ensure_extensions(documents: Path, repo_root: Path) -> None:
    """Symlink (or copy) the awesomecv ``_extensions`` tree into documents/.

    Leaves an existing link/dir untouched. Best-effort: a missing source just
    means quarto will surface its own clear extension error.
    """
    link = documents / "_extensions"
    if link.exists() or link.is_symlink():
        return
    src = _extensions_src(repo_root)
    if src is None:
        return
    try:
        os.symlink(src, link, target_is_directory=True)
    except OSError:
        with suppress(OSError):
            shutil.copytree(src, link)


# ---------------------------------------------------------------------------
# Quarto render (graceful — mirrors api/applications._render_cover_letter)
# ---------------------------------------------------------------------------


def _quarto_render(documents: Path) -> RenderResult:
    """Render resume.qmd -> resume.pdf. quarto absent => skipped (no fake PDF)."""
    quarto = shutil.which("quarto")
    if quarto is None:
        logger.info("apply_local render: quarto not on PATH — render skipped")
        return RenderResult(status="skipped", reason="quarto not installed")
    try:
        proc = subprocess.run(
            [quarto, "render", RESUME_QMD, "--to", "awesomecv-typst"],
            cwd=str(documents),
            capture_output=True,
            text=True,
            timeout=RENDER_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("apply_local render: quarto render failed: %s", exc)
        return RenderResult(status="error", reason=f"quarto render failed: {exc}")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        logger.warning("apply_local render: quarto exit %s: %s", proc.returncode, tail)
        return RenderResult(status="error", reason=f"quarto exit {proc.returncode}: {tail}")
    pdf = documents / RESUME_PDF
    return RenderResult(status="ok", pdf_path=str(pdf) if pdf.is_file() else None)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def render_local(slug: str, config: JobsmithConfig, *, repo_root: Any) -> RenderResult:
    """Build + render the resume and assemble the portfolio for ``slug``.

    NEVER raises: any unexpected failure is captured into a ``RenderResult`` so
    the caller's gather/draft artifacts are never lost.
    """
    try:
        return _render_local(slug, config, repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001 — render is best-effort; never crash the apply
        logger.warning("apply_local render: unexpected failure: %s", exc)
        return RenderResult(status="error", reason=f"render crashed: {exc}")


def _render_local(slug: str, config: JobsmithConfig, *, repo_root: Any) -> RenderResult:
    root = Path(repo_root) if repo_root is not None else Path.cwd()
    apps_dir = resolve(config.output.applications_dir, root)
    documents = apps_dir / slug / "documents"
    state = apply_state_dir(slug, root=root)

    err = _ensure_documents(documents, config, root)
    if err is not None:
        return RenderResult(status="error", reason=err)

    qmd = documents / RESUME_QMD
    if not qmd.exists():  # manual-edit safe — never clobber a hand-edited resume
        summary = _extract_professional_summary(_read_draft(state))
        qmd.write_text(_resume_qmd(_author_name(documents / "author.yml"), summary), encoding="utf-8")

    _ensure_extensions(documents, root)
    result = _quarto_render(documents)

    # Gate the resume's ACTUAL work bullets (work.yml) with the deterministic
    # prose-qa checks — prose-qa only saw prose-draft.md (roborev 1066).
    bullets_md = _resume_bullets_markdown(documents)
    if bullets_md:
        qa = run_prose_qa_checks(bullets_md, iteration=1)
        result.qa_pass = qa.get("decision") == "pass"
        result.qa_findings = qa.get("blocking_findings", [])
        if not result.qa_pass:
            logger.warning(
                "apply_local render: %d QA finding(s) on resume work.yml bullets",
                len(result.qa_findings),
            )

    result.artifacts.update(_doc_artifacts(documents))
    if result.pdf_path:
        result.artifacts["resume_pdf"] = result.pdf_path
    _assemble(slug, apps_dir, result)
    return result


def _read_draft(state: Path) -> str:
    path = state / ART_PROSE_DRAFT
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _resume_bullets_markdown(documents: Path) -> str:
    """The resume's work.yml bullets as markdown ``- `` lines for the QA checks."""
    work = documents / "work.yml"
    if not work.is_file():
        return ""
    try:
        data = yaml.safe_load(work.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return ""
    lines: list[str] = []
    for position in data if isinstance(data, list) else []:
        details = position.get("details") if isinstance(position, dict) else None
        for detail in details or []:
            text = detail.get("bullet", "") if isinstance(detail, dict) else str(detail)
            if text.strip():
                lines.append(f"- {text.strip()}")
    return "\n".join(lines)


def _doc_artifacts(documents: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, fname in (
        ("resume_qmd", RESUME_QMD),
        ("work_yml", "work.yml"),
        ("skill_yml", "skill.yml"),
        ("education_yml", "education.yml"),
        ("author_yml", "author.yml"),
    ):
        path = documents / fname
        if path.is_file():
            out[key] = str(path)
    return out


def _assemble(slug: str, apps_dir: Path, result: RenderResult) -> None:
    """Assemble the portfolio project (best-effort; failures captured, not raised)."""
    try:
        variables = assemble_application(slug, apps_dir)
        result.artifacts["variables_yml"] = str(variables)
    except Exception as exc:  # noqa: BLE001 — assembly is best-effort
        logger.warning("apply_local render: assemble_application failed: %s", exc)
        result.artifacts["assemble_error"] = str(exc)


__all__ = ["RenderResult", "render_local"]
