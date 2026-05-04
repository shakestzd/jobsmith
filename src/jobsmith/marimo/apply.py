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

__generated_with = "0.23.4"
app = marimo.App(width="medium")


@app.cell
def _imports():
    import os
    import sqlite3
    from pathlib import Path

    import marimo as mo

    from jobsmith.config import load_config
    from jobsmith.marimo.loader import ApplicationNotFound, load_sections

    return (
        ApplicationNotFound,
        Path,
        load_config,
        load_sections,
        mo,
        os,
        sqlite3,
    )


def _cli_flag_to_bool(value: object, *, default: bool) -> bool:
    """Normalize marimo's ``mo.cli_args()`` value for boolean-style flags.

    ``mo.cli_args()`` parses bare flags (e.g. ``--force``) as the empty
    string ``""``, not as ``True``. ``bool("")`` is ``False``, so the
    naive ``bool(value)`` check silently swallows the flag's intent
    (roborev #928 MEDIUM 1).

    Accepted truthy forms: any non-empty truthy string except an explicit
    ``"false"``/``"0"``/``"no"``, plus presence of the key with an empty
    string (bare ``--flag``).

    Accepted falsy forms: ``--flag false`` / ``--flag 0`` / ``--flag no``.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    s = str(value).strip().lower()
    if s == "":
        # Bare ``--flag`` with no value means "turn it on".
        return True
    return s not in {"false", "0", "no", "off"}


@app.cell
def _script_mode_args(Path, mo):
    """Read CLI args when invoked as ``python apply.py --url ... --jd-text-file ...``.

    In interactive mode (``marimo run apply.py`` or ``marimo edit apply.py``),
    ``mo.cli_args()`` returns an empty dict and ``cli_url`` is None — every
    consumer cell short-circuits, so the notebook behaves as a pure UI.

    In script mode, ``cli_url`` and ``cli_jd_text`` are populated and the
    ``_script_mode_runner`` cell below kicks off ``run_apply`` synchronously
    and exits the process. This makes the notebook a dual-mode entry point:

        marimo run apply.py                         # interactive UI
        python apply.py --url <URL>                 # CLI script
        python apply.py --url <URL> --jd-text-file <PATH>
    """
    _cli_args = mo.cli_args()
    cli_url: str | None = _cli_args.get("url")
    # Boolean flags must be normalized — bare ``--force`` parses to ""
    # and ``bool("")`` is ``False``, silently swallowing the flag's
    # intent (roborev #928 MEDIUM 1).
    cli_force: bool = _cli_flag_to_bool(_cli_args.get("force"), default=False)
    cli_yes: bool = _cli_flag_to_bool(_cli_args.get("yes"), default=True)
    cli_verbose: int = int(_cli_args.get("verbose", 0) or 0)

    cli_jd_text: str | None = None
    _cli_jd_text_file = _cli_args.get("jd-text-file")
    if _cli_jd_text_file:
        cli_jd_text = Path(_cli_jd_text_file).read_text(encoding="utf-8")
    elif _cli_args.get("jd-text"):
        cli_jd_text = _cli_args["jd-text"]
    return cli_force, cli_jd_text, cli_url, cli_verbose, cli_yes


@app.cell
def _repo_root_setup(Path, os):
    """Resolve repo_root WITHOUT touching the DB.

    The script-mode runner depends on ``repo_root`` (to pass to
    ``run_apply``) but must NOT depend on ``_db_setup``, which queries
    ``apply_runs`` and would crash on a fresh project where the DB has
    not been bootstrapped yet (roborev #928 MEDIUM 2). Splitting the
    resolution into its own cell keeps the script-mode entry independent
    of any DB state.
    """
    repo_root = Path(os.environ.get("JOBSMITH_REPO_ROOT", ".")).resolve()
    return (repo_root,)


def _read_distinct_slugs(db_path):
    """Return distinct slugs from ``apply_runs``, or [] if the DB / table
    is missing.

    Extracted for testability: the marimo cell wraps this helper so the
    fresh-project path (no ``private/jobsmith.db`` yet, or the table not
    bootstrapped) returns an empty list instead of crashing the cell
    graph (roborev #928 MEDIUM 2).
    """
    import sqlite3 as _sqlite3

    if not db_path.exists():
        return []
    _conn = _sqlite3.connect(str(db_path))
    try:
        _rows = _conn.execute(
            "SELECT DISTINCT slug FROM apply_runs ORDER BY slug"
        ).fetchall()
        return [_row[0] for _row in _rows] if _rows else []
    except _sqlite3.OperationalError:
        # apply_runs table not yet created — treat as no runs.
        return []
    finally:
        _conn.close()


@app.cell
def _db_setup(load_config, repo_root):
    _cfg = load_config()
    db_path = repo_root / _cfg.output.jobsmith_db
    # Resolve once from config so all cells (runner, re-run, loader) read
    # from / write to the same directory. Hardcoding "private/applications"
    # broke repos that override output.applications_dir (roborev #923 MED).
    apps_dir = repo_root / _cfg.output.applications_dir
    slugs = _read_distinct_slugs(db_path)
    return apps_dir, db_path, slugs


@app.cell
def _script_mode_runner(cli_force, cli_jd_text, cli_url, cli_verbose, cli_yes, repo_root):
    """Synchronous CLI entry point — fires only when ``--url`` is supplied.

    Calls ``run_apply`` directly (the existing CLI orchestration) and exits
    with its return code. Bypasses the threaded ``NotebookRunner`` because
    in script mode the process exits when all cells return — a background
    thread would be killed mid-pipeline.

    In UI mode (``cli_url is None``), the body is skipped and the
    interactive ``_run_dispatch`` cell takes over.
    """
    if cli_url is not None:
        import sys

        from jobsmith.apply import run_apply

        rc = run_apply(
            cli_url,
            cwd=repo_root,
            skip_confirm=cli_yes,
            force=cli_force,
            verbosity=cli_verbose,
            jd_text=cli_jd_text,
        )
        sys.exit(rc)


@app.cell
def _slug_picker(mo, slugs):
    # Note: mo.ui.dropdown does not accept a `placeholder` kwarg in
    # marimo>=0.22.4 — selecting nothing renders an empty value.
    slug_picker = mo.ui.dropdown(
        options=slugs,
        label="Application slug",
    )
    return (slug_picker,)


@app.cell
def _show_picker(mo, slug_picker):
    mo.vstack([slug_picker])
    return


@app.cell
def _run_controls(mo):
    url_input = mo.ui.text(
        placeholder="https://example.com/careers/role-id",
        label="Job URL (paste to run apply pipeline)",
    )
    # Optional JD text — for JS-rendered career portals (Netflix, some
    # Workday tenants) that WebFetch cannot scrape. When non-empty, the
    # gather orchestrator inlines this text into spec.json so
    # apply-jd-parser skips its WebFetch.
    jd_text_input = mo.ui.text_area(
        placeholder=(
            "Paste JD text here ONLY if the URL is JS-rendered "
            "(Netflix careers, etc.). Leave blank to scrape from the URL."
        ),
        label="JD text (optional, for JS-rendered portals)",
        rows=6,
    )
    run_button = mo.ui.run_button(label="Run apply")
    stop_button = mo.ui.run_button(label="Stop")
    return jd_text_input, run_button, stop_button, url_input


@app.cell
def _run_dispatch(
    apps_dir,
    db_path,
    jd_text_input,
    repo_root,
    run_button,
    stop_button,
    url_input,
):
    # Use the module-level singleton so the in-flight thread/queue/cancel_event
    # survive reactive recomputation. Constructing a new NotebookRunner per
    # cell re-run would orphan the running pipeline (roborev #920 HIGH).
    from jobsmith.marimo.runner import get_runner as _get_runner_d

    runner_state = _get_runner_d(
        db_path=db_path,
        applications_dir=apps_dir,
    )

    if run_button.value and url_input.value and not runner_state.is_running():
        # jd_text_input.value may be empty string when the user hasn't
        # pasted anything — pass None in that case so the runner doesn't
        # write a blank temp file.
        _jd_text = jd_text_input.value or None
        runner_state.start(url_input.value, cwd=repo_root, jd_text=_jd_text)

    if stop_button.value and runner_state.is_running():
        runner_state.cancel()
    return (runner_state,)


@app.cell
def _show_run_panel(
    jd_text_input,
    mo,
    run_button,
    runner_state,
    stop_button,
    url_input,
):
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
        jd_text_input,
        mo.hstack([run_button, stop_button]),
        mo.callout(
            mo.md(
                f"**Phase:** `{last_phase}` · **Status:** `{status}`\n\n"
                "_Live progress is per-phase; phases take 3–8 minutes._"
            ),
            kind="info",
        ),
    ])
    panel  # noqa: B018 — marimo renders the last expression of a cell
    return


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


@app.cell
def _chat_sidebar(chat_widget, mo, slug_picker):
    from jobsmith.marimo.chat_ui import render_chat_history_bubbles

    history: list[dict] = []  # populated from chat_messages on slug change
    history_accordion = mo.accordion(
        {"Conversation history": mo.vstack(render_chat_history_bubbles(history, mo))}
    )
    # mo.sidebar must be the cell's last expression to actually mount —
    # the previous version discarded the return value via a bare statement
    # wrapped in `if`, so the sidebar never showed up in the DOM.
    _sidebar_render = (
        mo.sidebar([history_accordion, chat_widget], width="480px")
        if slug_picker.value
        else None
    )
    _sidebar_render  # noqa: B018 — marimo renders the cell's last expression


@app.cell
def _parse_amendments(Path, chat_widget, repo_root, slug_picker):
    from jobsmith.marimo.directive_parser import parse_amendments
    from jobsmith.marimo.review_store import persist_amendment

    # amendments_by_section: section → list of (amendment_id, Amendment) tuples
    amendments_by_section: dict[str, list[tuple[str, object]]] = {}

    if slug_picker.value and chat_widget.value:
        _review_dir_p = Path(repo_root) / "private" / ".review"
        _slug_p = slug_picker.value

        # Scan all assistant messages in the current chat session
        for _msg in chat_widget.value:
            _role = getattr(_msg, "role", None) or (
                _msg.get("role", "") if isinstance(_msg, dict) else ""
            )
            if _role != "assistant":
                continue
            _content = getattr(_msg, "content", None) or (
                _msg.get("content", "") if isinstance(_msg, dict) else ""
            )
            if not _content:
                continue

            for _parsed in parse_amendments(_content):
                _stored_id = persist_amendment(_slug_p, _parsed, _review_dir_p)
                _section = _parsed.section
                if _section not in amendments_by_section:
                    amendments_by_section[_section] = []
                # Avoid duplicates in the in-memory list
                _existing = {aid for aid, _ in amendments_by_section[_section]}
                if _stored_id not in _existing:
                    # Rebuild amendment with the stored ID (may differ if deduped)
                    from copy import copy
                    _stored_amend = copy(_parsed)
                    _stored_amend.id = _stored_id
                    amendments_by_section[_section].append((_stored_id, _stored_amend))
    return (amendments_by_section,)


@app.cell
def _build_amendment_dropdowns(
    amendments_by_section: dict[str, list[tuple[str, object]]],
    mo,
):
    # amendment_dropdowns: amendment_id → mo.ui.dropdown instance
    amendment_dropdowns: dict[str, object] = {}

    for section_entries in amendments_by_section.values():
        for _aid, _amendment in section_entries:
            amendment_dropdowns[_aid] = mo.ui.dropdown(
                options=["Pending", "Accept", "Reject"],
                value="Pending",
                label=None,
            )
    return (amendment_dropdowns,)


@app.cell
def _sync_amendment_status(
    Path,
    amendment_dropdowns: dict[str, object],
    amendments_by_section: dict[str, list[tuple[str, object]]],
    repo_root,
    slug_picker,
):
    from jobsmith.marimo.review_store import set_status

    if slug_picker.value and amendment_dropdowns:
        _review_dir_s = Path(repo_root) / "private" / ".review"
        _slug_s = slug_picker.value

        # Flatten amendment_id → amendment mapping for lookup
        _id_to_amend: dict[str, object] = {}
        for _entries in amendments_by_section.values():
            for _aid_s, _amend_s in _entries:
                _id_to_amend[_aid_s] = _amend_s

        for _aid, _dropdown in amendment_dropdowns.items():
            _chosen = _dropdown.value
            if _chosen == "Accept":
                set_status(_slug_s, _aid, "accepted", _review_dir_s)
            elif _chosen == "Reject":
                set_status(_slug_s, _aid, "rejected", _review_dir_s)
            # "Pending" is the default — no DB write needed unless transitioning back
    return


@app.cell
def _load(ApplicationNotFound, apps_dir, db_path, load_sections, slug_picker):
    sections = None
    load_error = None

    if slug_picker.value:
        try:
            sections = load_sections(
                slug_picker.value, db_path, applications_dir=apps_dir
            )
        except ApplicationNotFound as exc:
            load_error = str(exc)
        except Exception as exc:  # noqa: BLE001
            load_error = f"Unexpected error: {exc}"
    return load_error, sections


@app.cell
def _render(
    amendment_dropdowns: dict[str, object],
    amendments_by_section: dict[str, list[tuple[str, object]]],
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
        for _aid_p, _amend_p in entries:
            _dropdown_p = amendment_dropdowns.get(_aid_p)
            if _dropdown_p is None:
                continue
            _op_label = f"[{_amend_p.op}]"
            _field_label = _amend_p.field or "(section)"
            rows.append(
                mo.hstack(
                    [
                        mo.md(f"`{_op_label}` **{_field_label}**: {_amend_p.value}"),
                        _dropdown_p,
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


@app.cell
def _show_accordion(accordion, mo):
    mo.vstack([accordion])
    return


@app.cell
def _rerun_buttons(mo):
    # Order matches gather → draft phases (render specialists write to
    # documents/, not .apply-state/, so they're not re-runnable here).
    rerun_specialists = (
        "apply-jd-parser",
        "apply-fit-scorer",
        "apply-hm-enricher",
        "apply-bullet-selector",
        "apply-company-research",
        "apply-prose-writer",
        "apply-prose-qa",
    )
    # Labels are the specialist suffix only (apply-jd-parser → jd-parser);
    # the "Re-run" verb lives in the section header above the row so the
    # buttons stay narrow enough not to wrap their hyphens at the
    # equally-distributed mo.hstack width.
    rerun_buttons = {
        _spec: mo.ui.run_button(label=_spec.removeprefix("apply-"))
        for _spec in rerun_specialists
    }
    return rerun_buttons, rerun_specialists


@app.cell
def _rerun_dispatch(
    apps_dir,
    db_path,
    repo_root,
    rerun_buttons,
    slug_picker,
    url_input,
):
    from jobsmith.marimo.runner import get_runner as _get_runner_r

    rerun_status = None
    _rerun_runner = _get_runner_r(
        db_path=db_path,
        applications_dir=apps_dir,
    )

    # Find the first button with .value=True. mo.ui.run_button latches per
    # click; we surface ONE re-run at a time to keep the runner singleton's
    # re-entry guard happy.
    if slug_picker.value and url_input.value:
        for _spec_name, _btn in rerun_buttons.items():
            if _btn.value and not _rerun_runner.is_running():
                _rerun_runner.run_specialist(
                    url=url_input.value,
                    specialist_name=_spec_name,
                    cwd=repo_root,
                )
                rerun_status = f"Re-running {_spec_name}…"
                break
    return (rerun_status,)


@app.cell
def _rerun_panel(mo, rerun_buttons, rerun_specialists, rerun_status):
    # justify="start" stops marimo from spreading 7 buttons across the full
    # page width — keeps each button at its natural label size and lets the
    # row wrap onto a second line on narrow viewports instead of squeezing.
    button_row = mo.hstack(
        [rerun_buttons[_n] for _n in rerun_specialists],
        justify="start",
        gap=0.5,
        wrap=True,
    )
    _rerun_blocks = [
        mo.md("**Re-run a single specialist** _(requires a URL above)_"),
        button_row,
    ]
    if rerun_status:
        _rerun_blocks.append(mo.callout(mo.md(rerun_status), kind="info"))
    mo.vstack(_rerun_blocks)
    return


@app.cell
def _finalize_button(mo):
    finalize_button = mo.ui.run_button(label="Finalize accepted edits")
    return (finalize_button,)


@app.cell
def _finalize_run(
    Path,
    chat_backend,
    finalize_button,
    load_config,
    repo_root,
    slug_picker,
):
    from jobsmith.marimo.finalize import finalize_run as _finalize_run_impl
    from jobsmith.marimo.review_store import set_status as _set_status

    finalize_result = None
    finalize_error = None

    if finalize_button.value and slug_picker.value:
        _cfg_f = load_config()
        _review_dir_f = Path(repo_root) / "private" / ".review"
        _apps_dir_f = Path(repo_root) / _cfg_f.output.applications_dir

        # Load accepted amendments from the review DB
        from jobsmith.db import open_review_db
        from jobsmith.marimo.directive_parser import Amendment
        _conn_f = open_review_db(slug_picker.value, _review_dir_f)
        try:
            _rows_f = _conn_f.execute(
                "SELECT amendment_id, section, op, value, status, "
                "target_index, target_field "
                "FROM amendments WHERE slug=? AND status='accepted'",
                (slug_picker.value,),
            ).fetchall()
        finally:
            _conn_f.close()

        _accepted_f = [
            Amendment(
                id=_r["amendment_id"],
                section=_r["section"],
                # target_index / target_field carry the parsed AMEND
                # work[0].bullet[2] target so finalize's YAML appliers
                # know where to write (roborev #921 HIGH).
                index=_r["target_index"],
                field=_r["target_field"],
                op=_r["op"],
                value=_r["value"],
                status="accepted",
            )
            for _r in _rows_f
        ]

        try:
            finalize_result = _finalize_run_impl(
                slug=slug_picker.value,
                accepted_amendments=_accepted_f,
                masters=_cfg_f.master,
                applications_dir=_apps_dir_f,
                review_db_dir=_review_dir_f,
                repo_root=Path(repo_root),
            )
            for _fid in finalize_result.finalized_amendment_ids:
                _set_status(slug_picker.value, _fid, "finalized", _review_dir_f)
            if chat_backend is not None and finalize_result.finalized_amendment_ids:
                chat_backend.start_new_session()
        except Exception as exc:  # noqa: BLE001 — surface to UI; do not crash notebook
            finalize_error = str(exc)
    return finalize_error, finalize_result


@app.cell
def _finalize_panel(finalize_button, finalize_error, finalize_result, mo):
    _final_blocks = [finalize_button]

    if finalize_error:
        _final_blocks.append(
            mo.callout(mo.md(f"**Finalize failed:** {finalize_error}"), kind="danger")
        )
    elif finalize_result is not None:
        _modified = finalize_result.modified_files or []
        _unsupported = finalize_result.unsupported_sections or []
        _lines = [
            f"**Backup:** `{finalize_result.backup_path}`",
            f"**Files written:** {len(_modified)}",
        ]
        for _f in _modified:
            _lines.append(f"- `{_f}`")
        if _unsupported:
            _lines.append(
                f"**Skipped (read-only sections):** {', '.join(_unsupported)}"
            )
        if finalize_result.finalized_amendment_ids:
            _lines.append(
                f"**Amendments finalized:** {len(finalize_result.finalized_amendment_ids)}"
            )
        if finalize_result.quarto_returncode != 0:
            _lines.append(
                f"**Quarto exit code:** {finalize_result.quarto_returncode} (PDF may be stale)"
            )
        _final_blocks.append(mo.callout(mo.md("\n\n".join(_lines)), kind="success"))

        if finalize_result.pdf_path is not None and finalize_result.pdf_path.exists():
            _final_blocks.append(mo.pdf(src=finalize_result.pdf_path))

    mo.vstack(_final_blocks)
    return


if __name__ == "__main__":
    app.run()
