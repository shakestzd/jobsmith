"""Marimo notebook: application runner + review.

Launch with:
    marimo edit src/jobsmith/marimo/apply.py

Slice 3 — slug picker + read-only section cards.
Slice 4 — URL input + Run apply + live phase-progress + flip to review.
Slice 5 — chat sidebar with ClaudeChatBackend.
Slice 6 — AMEND directive parser + SQLite staging + per-section dropdowns.
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
# Cell E0 — chat sidebar backend (slice 5)
# ---------------------------------------------------------------------------
@app.cell
def _chat_backend(Path, repo_root, slug_picker):
    from jobsmith.marimo.claude_chat import ClaudeChatBackend

    chat_backend = None
    if slug_picker.value:
        chat_backend = ClaudeChatBackend(
            slug=slug_picker.value,
            project_root=Path(repo_root),
            review_db_dir=Path(repo_root) / "private" / ".review",
            system_prompt=None,  # populated by next cell once sections load
        )
    return (chat_backend,)


# ---------------------------------------------------------------------------
# Cell E1 — chat model adapter for mo.ui.chat (slice 5)
# ---------------------------------------------------------------------------
@app.cell
def _chat_model(chat_backend, mo):
    def _model(messages, config):  # noqa: ARG001 — config required by mo.ui.chat
        if chat_backend is None or not messages:
            return ""
        last_user = messages[-1]
        text = getattr(last_user, "content", None) or last_user.get("content", "")
        # Drain the generator into the full reply; mo.ui.chat handles streaming
        return "".join(chat_backend.send(text))

    chat_widget = mo.ui.chat(_model)
    return (chat_widget,)


# ---------------------------------------------------------------------------
# Cell E2 — chat history accordion + sidebar mount (slice 5)
# ---------------------------------------------------------------------------
@app.cell
def _chat_sidebar(chat_widget, mo, slug_picker):
    from jobsmith.marimo.chat_ui import render_chat_history_bubbles

    history: list[dict] = []  # populated from chat_messages on slug change
    history_accordion = mo.accordion(
        {"Conversation history": mo.vstack(render_chat_history_bubbles(history, mo))}
    )
    if slug_picker.value:
        mo.sidebar([history_accordion, chat_widget], width="480px")
    return (history_accordion,)


# ---------------------------------------------------------------------------
# Cell E3 — parse AMEND directives from chat history → persist to review DB (slice 6)
# ---------------------------------------------------------------------------
@app.cell
def _parse_amendments(Path, chat_widget, repo_root, slug_picker):
    from jobsmith.marimo.directive_parser import parse_amendments
    from jobsmith.marimo.review_store import persist_amendment

    # amendments_by_section: section → list of (amendment_id, Amendment) tuples
    amendments_by_section: dict[str, list[tuple[str, object]]] = {}

    if slug_picker.value and chat_widget.value:
        review_db_dir = Path(repo_root) / "private" / ".review"
        slug = slug_picker.value

        # Scan all assistant messages in the current chat session
        for msg in chat_widget.value:
            role = getattr(msg, "role", None) or (
                msg.get("role", "") if isinstance(msg, dict) else ""
            )
            if role != "assistant":
                continue
            content = getattr(msg, "content", None) or (
                msg.get("content", "") if isinstance(msg, dict) else ""
            )
            if not content:
                continue

            for amendment in parse_amendments(content):
                stored_id = persist_amendment(slug, amendment, review_db_dir)
                section = amendment.section
                if section not in amendments_by_section:
                    amendments_by_section[section] = []
                # Avoid duplicates in the in-memory list
                existing_ids = {aid for aid, _ in amendments_by_section[section]}
                if stored_id not in existing_ids:
                    # Rebuild amendment with the stored ID (may differ if deduped)
                    from copy import copy
                    stored_amendment = copy(amendment)
                    stored_amendment.id = stored_id
                    amendments_by_section[section].append((stored_id, stored_amendment))

    return (amendments_by_section,)


# ---------------------------------------------------------------------------
# Cell E4 — build per-amendment dropdowns (slice 6)
# ---------------------------------------------------------------------------
@app.cell
def _build_amendment_dropdowns(amendments_by_section, mo):
    # amendment_dropdowns: amendment_id → mo.ui.dropdown instance
    amendment_dropdowns: dict[str, object] = {}

    for section_entries in amendments_by_section.values():
        for amendment_id, _amendment in section_entries:
            amendment_dropdowns[amendment_id] = mo.ui.dropdown(
                options=["Pending", "Accept", "Reject"],
                value="Pending",
                label=None,
            )

    return (amendment_dropdowns,)


# ---------------------------------------------------------------------------
# Cell E5 — sync dropdown changes back to the review DB (slice 6)
# ---------------------------------------------------------------------------
@app.cell
def _sync_amendment_status(
    Path, amendment_dropdowns, amendments_by_section, repo_root, slug_picker
):
    from jobsmith.marimo.review_store import set_status

    if slug_picker.value and amendment_dropdowns:
        review_db_dir = Path(repo_root) / "private" / ".review"
        slug = slug_picker.value

        # Flatten amendment_id → amendment mapping for lookup
        _id_to_amendment: dict[str, object] = {}
        for entries in amendments_by_section.values():
            for aid, amendment in entries:
                _id_to_amendment[aid] = amendment

        for amendment_id, dropdown in amendment_dropdowns.items():
            chosen = dropdown.value
            if chosen == "Accept":
                set_status(slug, amendment_id, "accepted", review_db_dir)
            elif chosen == "Reject":
                set_status(slug, amendment_id, "rejected", review_db_dir)
            # "Pending" is the default — no DB write needed unless transitioning back


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
# Cell F — render section cards (with amendment dropdowns, slice 6)
# ---------------------------------------------------------------------------
@app.cell
def _render(
    amendment_dropdowns,
    amendments_by_section,
    load_error,
    mo,
    sections,
    slug_picker,
):
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

    def _amendment_panel(section_key: str) -> object:
        """Render pending amendment dropdowns for *section_key*, or empty vstack."""
        entries = amendments_by_section.get(section_key, [])
        if not entries:
            return mo.vstack([])
        rows = []
        for aid, amendment in entries:
            dropdown = amendment_dropdowns.get(aid)
            if dropdown is None:
                continue
            op_label = f"[{amendment.op}]"
            field_label = amendment.field or "(section)"
            rows.append(
                mo.hstack(
                    [
                        mo.md(f"`{op_label}` **{field_label}**: {amendment.value}"),
                        dropdown,
                    ],
                    gap="1rem",
                    align="center",
                )
            )
        if not rows:
            return mo.vstack([])
        return mo.vstack(
            [mo.md("**Pending amendments:**"), *rows],
        )

    def _section_card(content_el: object, section_key: str) -> object:
        """Combine content element with its amendment panel."""
        panel = _amendment_panel(section_key)
        return mo.vstack([content_el, panel])

    _s = sections  # alias for brevity
    accordion = mo.accordion(
        {
            "Work Bullets": _section_card(
                mo.md(_bullets_md(_s.work_bullets if _s else None)), "work"
            ),
            "Fit Score": _section_card(
                mo.md(_fit_md(_s.fit_score if _s else None)), "fit-score"
            ),
            "HM Snippet": mo.md(_hm_md(_s.hm_snippet if _s else None)),
            "Cover Letter": _section_card(
                mo.md(_cover_md(_s.cover_letter if _s else None)), "cover-letter"
            ),
            "ATS Check": mo.md(_ats_md(_s.ats_check if _s else None)),
            "Prose Draft": mo.md(_prose_md(_s.prose_draft if _s else None)),
            "Skills": _section_card(
                mo.md(placeholder), "skills"
            ),
            "Education": _section_card(
                mo.md(placeholder), "education"
            ),
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
