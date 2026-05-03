"""Marimo notebook: application runner + review.

Launch with:
    marimo edit src/jobsmith/marimo/apply.py

Slice 3 — slug picker + read-only section cards.
Slice 4 — URL input + Run apply + live phase-progress + flip to review.

Subsequent slices (5–7) add chat sidebar, amendment dropdowns, and
the Finalize/PDF render path.
"""
# ruff: noqa: N803, N806, N818

import marimo

__generated_with = "0.22.4"
app = marimo.App(width="medium")


# ---------------------------------------------------------------------------
# Cell A — imports
# ---------------------------------------------------------------------------
@app.cell
def _imports():
    import os
    import sqlite3
    from pathlib import Path

    import marimo as mo

    from jobsmith.config import load_config
    from jobsmith.marimo.loader import ApplicationNotFound, load_sections

    return ApplicationNotFound, Path, load_config, load_sections, mo, os, sqlite3


# ---------------------------------------------------------------------------
# Cell B — locate DB and read available slugs
# ---------------------------------------------------------------------------
@app.cell
def _db_setup(Path, load_config, os, sqlite3):
    cfg = load_config()
    repo_root = Path(os.environ.get("JOBSMITH_REPO_ROOT", ".")).resolve()
    db_path = repo_root / cfg.output.jobsmith_db

    _conn = sqlite3.connect(str(db_path))
    _rows = _conn.execute(
        "SELECT DISTINCT slug FROM apply_runs ORDER BY slug"
    ).fetchall()
    _conn.close()

    slugs = [row[0] for row in _rows] if _rows else []
    return db_path, repo_root, slugs


# ---------------------------------------------------------------------------
# Cell C — slug picker dropdown
# ---------------------------------------------------------------------------
@app.cell
def _slug_picker(mo, slugs):
    slug_picker = mo.ui.dropdown(
        options=slugs,
        label="Application slug",
        placeholder="Select an application…",
    )
    return (slug_picker,)


# ---------------------------------------------------------------------------
# Cell D — show slug picker
# ---------------------------------------------------------------------------
@app.cell
def _show_picker(mo, slug_picker):
    mo.vstack([slug_picker])  # noqa: B018


# ---------------------------------------------------------------------------
# Cell D2 — run-mode: URL input + Run/Stop buttons (slice 4)
# ---------------------------------------------------------------------------
@app.cell
def _run_controls(Path, mo, repo_root):
    from jobsmith.marimo.runner import NotebookRunner

    url_input = mo.ui.text(
        placeholder="https://example.com/careers/role-id",
        label="Job URL (paste to run apply pipeline)",
    )
    run_button = mo.ui.run_button(label="Run apply")
    stop_button = mo.ui.run_button(label="Stop")
    return NotebookRunner, run_button, stop_button, url_input


# ---------------------------------------------------------------------------
# Cell D3 — runner state + dispatch (slice 4)
# ---------------------------------------------------------------------------
@app.cell
def _run_dispatch(
    NotebookRunner,
    Path,
    db_path,
    repo_root,
    run_button,
    stop_button,
    url_input,
):
    # One runner per notebook session
    runner_state = NotebookRunner(
        db_path=db_path,
        applications_dir=Path(repo_root) / "private" / "applications",
    )

    if run_button.value and url_input.value and not runner_state.is_running():
        runner_state.start(url_input.value, cwd=repo_root)

    if stop_button.value and runner_state.is_running():
        runner_state.cancel()

    return (runner_state,)


# ---------------------------------------------------------------------------
# Cell D4 — show controls + live phase progress (slice 4)
# ---------------------------------------------------------------------------
@app.cell
def _show_run_panel(mo, run_button, runner_state, stop_button, url_input):
    # Phase progress is drained from the runner queue. This cell re-runs
    # whenever runner_state changes or buttons are clicked.
    last_phase = "—"
    status = "idle"
    while not runner_state.events_queue.empty():
        ev = runner_state.events_queue.get_nowait()
        # ev is a PipelineEvent or _Done sentinel
        kind = getattr(ev, "kind", None) or getattr(ev, "status", None)
        if kind in ("phase_started", "phase_complete"):
            last_phase = ev.phase
            status = kind
        elif kind in ("done", "cancelled", "failed"):
            status = kind

    panel = mo.vstack([
        url_input,
        mo.hstack([run_button, stop_button]),
        mo.callout(
            mo.md(
                f"**Phase:** `{last_phase}` · **Status:** `{status}`\n\n"
                "_Live progress is per-phase; phases take 3–8 minutes._"
            ),
            kind="info",
        ),
    ])
    panel  # noqa: B018


# ---------------------------------------------------------------------------
# Cell E — load sections for selected slug
# ---------------------------------------------------------------------------
@app.cell
def _load(ApplicationNotFound, db_path, load_sections, slug_picker):
    sections = None
    load_error = None

    if slug_picker.value:
        try:
            sections = load_sections(slug_picker.value, db_path)
        except ApplicationNotFound as exc:
            load_error = str(exc)
        except Exception as exc:  # noqa: BLE001
            load_error = f"Unexpected error: {exc}"

    return load_error, sections


# ---------------------------------------------------------------------------
# Cell F — render section cards
# ---------------------------------------------------------------------------
@app.cell
def _render(load_error, mo, sections, slug_picker):
    placeholder = "_Section not yet generated._"

    if not slug_picker.value:
        mo.stop(True, mo.md("Select a slug above to review an application."))

    if load_error:
        mo.stop(True, mo.callout(mo.md(f"**Error:** {load_error}"), kind="danger"))

    def _fit_md(fit) -> str:
        if fit is None:
            return placeholder
        score_pct = f"{fit.score * 100:.0f}%" if fit.score is not None else "N/A"
        lines = [
            f"**Score:** {score_pct}  ",
            f"**Specialty:** {fit.specialty or 'N/A'}  ",
            f"**Confidence:** {fit.confidence or 'N/A'}  ",
            "",
            f"**Rationale:** {fit.rationale or ''}",
        ]
        if fit.pitch:
            lines += ["", f"**Pitch:** {fit.pitch}"]
        if fit.concerns:
            lines += ["", "**Concerns:**"] + [f"- {c}" for c in fit.concerns]
        return "\n".join(lines)

    def _bullets_md(bullets) -> str:
        if bullets is None:
            return placeholder
        kept = bullets.anchor_bullets_kept or []
        if not kept:
            return "_No anchor bullets selected yet._"
        return "\n".join(f"- {b}" for b in kept)

    def _hm_md(hm) -> str:
        if hm is None:
            return placeholder
        if not hm.detected:
            return "_No hiring manager signal detected._"
        lines = [
            f"**Name:** {hm.name or 'Unknown'}  ",
            f"**Source:** {hm.source or 'N/A'}  ",
            "",
            f"**Signal:** {hm.one_specific_signal or ''}",
        ]
        if hm.suggested_hook:
            lines += ["", f"**Suggested hook:** {hm.suggested_hook}"]
        return "\n".join(lines)

    def _ats_md(ats) -> str:
        if ats is None:
            return placeholder
        score_pct = f"{ats.score * 100:.0f}%" if ats.score is not None else "N/A"
        lines = [f"**ATS Score:** {score_pct}"]
        if ats.issues:
            lines += ["", "**Issues:**"] + [f"- {i}" for i in ats.issues]
        if ats.suggestions:
            lines += ["", "**Suggestions:**"] + [f"- {s}" for s in ats.suggestions]
        return "\n".join(lines)

    def _prose_md(prose) -> str:
        if prose is None:
            return placeholder
        return prose.text or "_Empty draft._"

    def _cover_md(cover) -> str:
        if cover is None:
            return placeholder
        return cover

    _s = sections  # alias for brevity
    accordion = mo.accordion(
        {
            "Work Bullets": mo.md(_bullets_md(_s.work_bullets if _s else None)),
            "Fit Score": mo.md(_fit_md(_s.fit_score if _s else None)),
            "HM Snippet": mo.md(_hm_md(_s.hm_snippet if _s else None)),
            "Cover Letter": mo.md(_cover_md(_s.cover_letter if _s else None)),
            "ATS Check": mo.md(_ats_md(_s.ats_check if _s else None)),
            "Prose Draft": mo.md(_prose_md(_s.prose_draft if _s else None)),
        }
    )
    return (accordion,)


# ---------------------------------------------------------------------------
# Cell G — show accordion
# ---------------------------------------------------------------------------
@app.cell
def _show_accordion(accordion, mo):
    mo.vstack([accordion])


if __name__ == "__main__":
    app.run()
