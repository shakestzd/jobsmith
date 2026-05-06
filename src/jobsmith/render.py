"""render.py — rich-rendered terminal output helpers for `jobsmith apply`.

Provides :class:`ApplyRenderer` which encapsulates all rich Console/Progress
interactions for the three-phase apply pipeline.

Design goals
------------
- Phase headers as cyan ``rich.panel.Panel``
- Per-phase ``rich.progress.Progress`` spinner with rolling status (quiet mode)
- Verbosity levels: 0=quiet, 1=-v (filtered), 2=-vv (unfiltered)
- Tool calls as ``[cyan]→[/] [bold]Tool[/]([dim]args[/])``
- Tool results as ``[dim]← result…[/]``
- Phase complete / failed as styled summary panels
- Non-TTY fallback: plain line-by-line output, no spinners
- Transcript JSONL always written to .apply-state/transcript.jsonl
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from io import IOBase
from pathlib import Path
from typing import TextIO

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from . import headless

# Max chars for truncated args / result previews
_MAX_ARG_CHARS = 80
_MAX_RESULT_CHARS = 100
_MAX_SPINNER_CHARS = 60

# Number of key=value pairs to show in tool args
_MAX_KV_PAIRS = 3

# Priority keys to look at first when summarising tool_input
_PRIORITY_KEYS = ("command", "path", "file_path", "url", "query", "input")

# Path-shaped keys whose values should receive mid-truncation
_PATH_KEYS = {"path", "file_path"}

# Tool names to filter entirely (no output for tool_use or its tool_result)
_FILTERED_TOOLS = {"TodoWrite", "ToolSearch"}

# Verbosity levels
VERBOSITY_QUIET = 0
VERBOSITY_VERBOSE = 1
VERBOSITY_DEBUG = 2


def _now_iso() -> str:
    """Return current time as ISO 8601 string with UTC offset."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _truncate_path(s: str, max_chars: int = 50) -> str:
    """Mid-truncate a path string preserving the first ~20 and last ~30 characters.

    If the string is shorter than *max_chars*, it is returned unchanged.
    The truncation uses ``…`` as the join marker.

    Parameters
    ----------
    s:
        The path string to truncate.
    max_chars:
        Maximum total length of the returned string (including the ``…``).
    """
    if len(s) <= max_chars:
        return s
    # Reserve one char for the ellipsis
    budget = max_chars - 1
    head = max(budget // 3, 10)
    tail = budget - head
    return s[:head] + "…" + s[-tail:]


def _is_line_numbered(text: str) -> bool:
    """Return True if *text* looks like line-numbered file content.

    A line-numbered file has at least two lines matching ``^\\s*\\d+\\s+\\S``.
    """
    pattern = re.compile(r"^\s*\d+\s+\S")
    matching = sum(1 for line in text.splitlines() if pattern.match(line))
    return matching >= 2


def _summarise_result(result: str, max_chars: int) -> str:
    """Return a concise summary of *result*.

    Priority order:
    1. JSON object  → ``{N keys}``
    2. JSON array   → ``[N items]``
    3. Line-numbered file content → ``N lines (M.K KB)``
    4. Fallback: single-line preview truncated to *max_chars*
    """
    stripped = result.strip()

    # JSON object or array detection
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return f"{{{len(parsed)} keys}}"
            if isinstance(parsed, list):
                return f"[{len(parsed)} items]"
        except (json.JSONDecodeError, ValueError):
            pass

    # Line-numbered file content
    if _is_line_numbered(result):
        n_lines = len(result.splitlines())
        kb = len(result.encode()) / 1024
        return f"{n_lines} lines ({kb:.1f} KB)"

    # Fallback: single-line preview
    one_line = result.replace("\n", " ").strip()
    if len(one_line) > max_chars:
        return one_line[: max_chars - 1] + "…"
    return one_line


def _format_tool_args(tool_input: dict | None, max_chars: int) -> str:
    """Summarise *tool_input* as ``key=value, …`` truncated to *max_chars*.

    Path-shaped values (keys in ``_PATH_KEYS``) are mid-truncated so the
    filename is always preserved.
    """
    if not tool_input:
        return ""

    pairs: list[str] = []
    remaining_keys = list(tool_input.keys())

    # Priority keys first
    for key in _PRIORITY_KEYS:
        if key in tool_input:
            val = str(tool_input[key])
            if key in _PATH_KEYS and (val.startswith("/") or "/" in val):
                # Mid-truncate path values
                val_display = _truncate_path(val, max_chars=50)
            else:
                val_display = val[:50] if len(val) > 50 else val
            pairs.append(f"{key}={val_display!r}")
            remaining_keys.remove(key)
            if len(pairs) >= _MAX_KV_PAIRS:
                break

    # Fill remaining slots with other keys
    for key in remaining_keys:
        if len(pairs) >= _MAX_KV_PAIRS:
            break
        val = str(tool_input[key])
        val_display = val[:40] if len(val) > 40 else val
        pairs.append(f"{key}={val_display!r}")

    summary = ", ".join(pairs)
    if len(summary) > max_chars:
        summary = summary[: max_chars - 1] + "…"
    return summary


def _format_result(result: str | None, max_chars: int) -> str:
    """Return a single-line preview of *result* truncated to *max_chars*."""
    if not result:
        return ""
    return _summarise_result(result, max_chars)


def _extract_tool_use_id(event: headless.Event) -> str | None:
    """Extract the tool_use block ``id`` from a tool_use event's raw payload.

    Returns None if the id cannot be found.
    """
    raw = event.raw or {}
    message = raw.get("message", raw)
    content = message.get("content", [])
    if not isinstance(content, list):
        return None
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "tool_use"
            and block.get("name") == event.tool_name
        ):
            return block.get("id")
    return None


class ApplyRenderer:
    """Renders apply-pipeline events to the terminal using rich.

    Parameters
    ----------
    yes:
        When True, suppress the progress spinner (--yes / CI-unattended mode).
        Phase panels and event lines are still rendered.
    verbosity:
        0 = quiet (default): phase panels + rolling spinner + sub-agent lines only.
        1 = -v: also shows tool calls/results (filtered: no TodoWrite/ToolSearch).
        2 = -vv: shows everything including TodoWrite, ToolSearch (dim styled).
    console:
        Optionally supply a pre-built Console (useful for tests).  When None,
        one is constructed automatically with TTY-aware defaults.
    """

    def __init__(
        self,
        *,
        yes: bool = False,
        verbosity: int = VERBOSITY_QUIET,
        console: Console | None = None,
    ) -> None:
        if console is not None:
            self.console = console
        else:
            # Non-TTY detection: if stderr is not a TTY, disable markup and
            # colour so piped / CI output stays clean.
            is_tty = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
            self.console = Console(
                stderr=True,
                highlight=False,
                markup=True,
                no_color=not is_tty,
            )

        self._yes = yes
        self._verbosity = verbosity
        self._use_spinner = (
            (not yes)
            and self.console.is_terminal
            and verbosity == VERBOSITY_QUIET
        )
        self._progress: Progress | None = None
        self._progress_task_id = None
        # Track tool_use_ids for filtered tools so we can drop their tool_results
        self._filtered_tool_use_ids: set[str] = set()
        # Track whether we're currently inside an Agent dispatch
        self._inside_agent: bool = False
        # Sub-agent dispatch timing: name -> start time
        self._agent_dispatch_times: dict[str, float] = {}
        # Current phase name (for transcript)
        self._current_phase: str | None = None
        # Transcript file handle (append-only, opened per phase)
        self._transcript_fh: TextIO | None = None
        self._transcript_path: Path | None = None
        # trk-60217f9f Pass 4: every transcript record also lands in
        # apply_state_log so the supervisor can tail by row id instead of
        # file offset (Pass 5 removes the disk file). Set by
        # ``open_transcript`` when a slug + cwd are passed; None otherwise.
        self._transcript_slug: str | None = None
        self._transcript_db_path: Path | None = None

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def print_header(self, phase_num: int, total: int, phase_name: str) -> None:
        """Render the phase header panel."""
        title = f"Phase {phase_num} / {total} — {phase_name.capitalize()}"
        self.console.print(Panel(f"[bold]{title}[/bold]", style="cyan", expand=False))

    def start_phase(self, phase_name: str) -> None:
        """Start the spinner for a new phase (no-op in non-spinner mode)."""
        self._current_phase = phase_name
        if not self._use_spinner:
            return
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=self.console,
            transient=False,
        )
        self._progress.start()
        task_id = self._progress.add_task(
            description=f"Running {phase_name}…", total=None
        )
        self._progress_task_id = task_id

    def stop_phase(self) -> None:
        """Stop the spinner (no-op if not running)."""
        if self._progress is not None:
            self._progress.stop()
            self._progress = None
            self._progress_task_id = None

    def open_transcript(
        self,
        transcript_path: Path,
        phase_name: str,
        *,
        slug: str | None = None,
        db_path: Path | None = None,
    ) -> None:
        """Open the transcript JSONL file for append and write a phase boundary marker.

        Parameters
        ----------
        transcript_path:
            Absolute path to the transcript.jsonl file.
        phase_name:
            Current phase name (used in the boundary marker).
        slug, db_path:
            When both are provided, every transcript record is also appended
            to ``apply_state_log`` (trk-60217f9f Pass 4). Disk + DB run in
            parallel during the migration window so existing tailers (file
            offset) and new tailers (DB row id) see identical streams.
        """
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        self._transcript_path = transcript_path
        self._transcript_fh = transcript_path.open("a", encoding="utf-8")
        self._transcript_slug = slug
        self._transcript_db_path = db_path
        # Write phase boundary marker (disk + DB)
        marker = {"_phase_boundary": phase_name, "ts": _now_iso()}
        self._transcript_fh.write(json.dumps(marker) + "\n")
        self._transcript_fh.flush()
        self._append_state_log(marker)

    def close_transcript(self) -> None:
        """Flush and close the transcript file handle."""
        if self._transcript_fh is not None:
            try:
                self._transcript_fh.flush()
                self._transcript_fh.close()
            except OSError:
                pass
            self._transcript_fh = None
            self._transcript_path = None
        self._transcript_slug = None
        self._transcript_db_path = None

    def _append_state_log(self, record: dict) -> None:
        """Mirror *record* into ``apply_state_log`` (best-effort, never raises).

        Pass 4 dual-write. When the renderer was opened without a slug + DB
        path (CLI-only contexts, tests) this is a no-op. The disk file
        remains the canonical store until Pass 5 removes it.
        """
        slug = self._transcript_slug
        db_path = self._transcript_db_path
        if slug is None or db_path is None:
            return
        try:
            from .db import append_state_log, open_pipeline_db

            conn = open_pipeline_db(db_path)
            try:
                append_state_log(conn, slug=slug, payload=json.dumps(record))
            finally:
                conn.close()
        except (OSError, sqlite3.Error):
            # Never let a DB hiccup break the pipeline — disk is the
            # source of truth for the duration of the Pass 4 migration.
            pass

    def _write_transcript(self, record: dict) -> None:
        """Append a JSON record to the transcript file + apply_state_log."""
        if self._transcript_fh is None:
            return
        try:
            self._transcript_fh.write(json.dumps(record) + "\n")
            self._transcript_fh.flush()
        except OSError:
            pass
        self._append_state_log(record)

    def update_status(self, text: str) -> None:
        """Update the rolling spinner status line (quiet mode only)."""
        if self._progress is None or self._progress_task_id is None:
            return
        width = self.console.width or 120
        prefix_width = 4  # spinner + space + prefix
        max_len = max(width - prefix_width, 20)
        if len(text) > max_len:
            text = text[: max_len - 1] + "…"
        self._progress.update(self._progress_task_id, description=text)

    def render_event(self, event: headless.Event) -> None:
        """Render a single event to the console, gated by verbosity."""
        width = self.console.width or 120
        phase = self._current_phase or "unknown"

        if event.type == "tool_use":
            name = event.tool_name or "?"
            is_filtered_tool = name in _FILTERED_TOOLS

            # Capture tool_use_id for filtered tools so we can drop their results
            if is_filtered_tool:
                tool_use_id = _extract_tool_use_id(event)
                if tool_use_id:
                    self._filtered_tool_use_ids.add(tool_use_id)

            # Write transcript always
            raw_dict = event.raw if isinstance(event.raw, dict) else {}
            tool_input_preview = ""
            if event.tool_input:
                try:
                    tool_input_preview = json.dumps(event.tool_input)[:200]
                except (TypeError, ValueError):
                    tool_input_preview = str(event.tool_input)[:200]
            self._write_transcript(
                {
                    "ts": _now_iso(),
                    "phase": phase,
                    "type": "tool_call",
                    "tool_name": name,
                    "tool_input_truncated": tool_input_preview,
                    "raw": raw_dict,
                }
            )

            args = _format_tool_args(event.tool_input, max_chars=width - 12)

            # Agent dispatch handling
            if name == "Agent":
                self._inside_agent = True
                indent = ""
                # Record dispatch time for duration calculation
                agent_name = ""
                if event.tool_input:
                    agent_name = str(event.tool_input.get("name", ""))
                self._agent_dispatch_times[agent_name] = time.monotonic()
                # Always print sub-agent dispatch line (all verbosity levels)
                self.stop_phase()
                self.console.print(
                    f"[cyan]→[/cyan] Agent([bold]{agent_name}[/bold])"
                )
                # Restart spinner after printing dispatch line
                if self._use_spinner and self._progress is None:
                    self._start_spinner_for_current_phase()
                return

            # Non-Agent tool_use handling
            if self._inside_agent:
                self._inside_agent = False
                indent = ""
            else:
                indent = ""

            # Quiet mode: update spinner, don't print tool call line
            if self._verbosity == VERBOSITY_QUIET:
                self.update_status(f"[cyan]→[/cyan] {name}…")
                return

            # -v: show filtered tools only
            if self._verbosity == VERBOSITY_VERBOSE:
                if is_filtered_tool:
                    return
                self.console.print(
                    f"{indent}[cyan]→[/cyan] [bold]{name}[/bold]([dim]{args}[/dim])"
                )
                return

            # -vv: show everything, dim filtered tools
            if is_filtered_tool:
                self.console.print(
                    f"{indent}[dim][cyan]→[/cyan] [bold]{name}[/bold]({args})[/dim]"
                )
            else:
                self.console.print(
                    f"{indent}[cyan]→[/cyan] [bold]{name}[/bold]([dim]{args}[/dim])"
                )

        elif event.type == "tool_result":
            # tool_result: tool_name stores the tool_use_id in headless.py
            tool_use_id = event.tool_name

            # Write transcript always
            raw_dict = event.raw if isinstance(event.raw, dict) else {}
            result_preview = (event.tool_result or "")[:200]
            self._write_transcript(
                {
                    "ts": _now_iso(),
                    "phase": phase,
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "result_truncated": result_preview,
                    "raw": raw_dict,
                }
            )

            # Check if result corresponds to an Agent completing
            # Best-effort: check agent dispatch times for sub-agent completion
            if self._agent_dispatch_times:
                # Sub-agent result: print completion line with duration
                # We emit for the most recently dispatched agent (heuristic)
                agent_names = list(self._agent_dispatch_times.keys())
                if agent_names:
                    last_agent = agent_names[-1]
                    elapsed = time.monotonic() - self._agent_dispatch_times.pop(last_agent)
                    elapsed_s = int(elapsed)
                    self.stop_phase()
                    self.console.print(
                        f"[green]✓[/green] {last_agent} ({elapsed_s}s)"
                    )
                    if self._use_spinner and self._progress is None:
                        self._start_spinner_for_current_phase()
                    return

            # Filter: drop results whose tool_use_id was filtered
            if tool_use_id and tool_use_id in self._filtered_tool_use_ids:
                return

            # Quiet mode: don't print result lines
            if self._verbosity == VERBOSITY_QUIET:
                return

            # -v: filtered (already handled above via _filtered_tool_use_ids)
            if self._verbosity == VERBOSITY_VERBOSE:
                if tool_use_id and tool_use_id in self._filtered_tool_use_ids:
                    return
                preview = _format_result(event.tool_result, max_chars=_MAX_RESULT_CHARS)
                self.console.print(f"[dim]← {preview}[/dim]")
                return

            # -vv: show everything
            preview = _format_result(event.tool_result, max_chars=_MAX_RESULT_CHARS)
            self.console.print(f"[dim]← {preview}[/dim]")

        elif event.type == "text" and event.text:
            # Write transcript always
            raw_dict = event.raw if isinstance(event.raw, dict) else {}
            self._write_transcript(
                {
                    "ts": _now_iso(),
                    "phase": phase,
                    "type": "text",
                    "text_truncated": (event.text or "")[:200],
                    "raw": raw_dict,
                }
            )

            # Only print text in non-quiet modes
            if self._verbosity > VERBOSITY_QUIET:
                stripped = event.text.strip()
                if stripped:
                    self.console.print(f"[dim italic]{stripped}[/dim italic]")

        elif event.type == "error":
            raw_dict = event.raw if isinstance(event.raw, dict) else {}
            self._write_transcript(
                {
                    "ts": _now_iso(),
                    "phase": phase,
                    "type": "error",
                    "error": event.error,
                    "raw": raw_dict,
                }
            )
            self.stop_phase()
            self.console.print(f"[red]✗ {event.error}[/red]")

        elif event.type == "phase_complete":
            raw_dict = event.raw if isinstance(event.raw, dict) else {}
            self._write_transcript(
                {
                    "ts": _now_iso(),
                    "phase": phase,
                    "type": "phase_complete",
                    "name": event.name,
                    "raw": raw_dict,
                }
            )
            self.stop_phase()
            name = event.name or "?"
            self.console.print(
                Panel(
                    f"[bold]✓ Phase {name} complete[/bold]",
                    style="green",
                    expand=False,
                )
            )

        elif event.type == "phase_failed":
            raw_dict = event.raw if isinstance(event.raw, dict) else {}
            self._write_transcript(
                {
                    "ts": _now_iso(),
                    "phase": phase,
                    "type": "phase_failed",
                    "name": event.name,
                    "error": event.error,
                    "raw": raw_dict,
                }
            )
            self.stop_phase()
            name = event.name or "?"
            reason = f"\n[dim]{event.error}[/dim]" if event.error else ""
            self.console.print(
                Panel(
                    f"[bold]✗ Phase {name} failed[/bold]{reason}",
                    style="red",
                    expand=False,
                )
            )

    def _start_spinner_for_current_phase(self) -> None:
        """Re-start the spinner after printing a persistent sub-agent line."""
        if not self._use_spinner or self._current_phase is None:
            return
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=self.console,
            transient=False,
        )
        self._progress.start()
        task_id = self._progress.add_task(
            description=f"Running {self._current_phase}…", total=None
        )
        self._progress_task_id = task_id

    def render_phase_summary(self, phase_name: str, apply_state_dir: Path) -> None:
        """Render a summary panel of gather-phase artifacts before the confirm gate.

        Reads the following artifacts from *apply_state_dir* (all optional):
        - ``fit-score.json`` — must_have_table with requirement/evidence/met
        - ``jd-parsed.json`` — must_have list of requirements
        - ``anchor-bullet-diff.md`` or ``bullet-diff.md`` — kept/dropped summary
        - ``hm-snippet.md`` — HM detection snippet

        Missing files are silently skipped.
        """
        rows: list[str] = []
        tables: list[Table] = []

        # --- fit-score.json ---
        fit_path = apply_state_dir / "fit-score.json"
        if fit_path.exists():
            try:
                fit_data = json.loads(fit_path.read_text())
                must_have = fit_data.get("must_have_table", [])
                if must_have:
                    tbl = Table(title="Fit Score — Must Have", show_lines=False)
                    tbl.add_column("Requirement")
                    tbl.add_column("Evidence")
                    tbl.add_column("Met")
                    for row in must_have:
                        met_str = "✓" if row.get("met") else "✗"
                        tbl.add_row(
                            str(row.get("requirement", "")),
                            str(row.get("evidence", "")),
                            met_str,
                        )
                    tables.append(tbl)
            except (json.JSONDecodeError, OSError):
                pass

        # --- jd-parsed.json ---
        jd_path = apply_state_dir / "jd-parsed.json"
        if jd_path.exists():
            try:
                jd_data = json.loads(jd_path.read_text())
                must_have = jd_data.get("must_have", [])
                if must_have:
                    tbl = Table(title="JD — Must Have Requirements", show_lines=False)
                    tbl.add_column("Requirement")
                    for req in must_have:
                        if isinstance(req, dict):
                            tbl.add_row(str(req.get("requirement", req)))
                        else:
                            tbl.add_row(str(req))
                    tables.append(tbl)
            except (json.JSONDecodeError, OSError):
                pass

        # --- bullet-diff.md (try both names) ---
        for diff_name in ("anchor-bullet-diff.md", "bullet-diff.md"):
            diff_path = apply_state_dir / diff_name
            if diff_path.exists():
                try:
                    diff_text = diff_path.read_text().strip()
                    if diff_text:
                        rows.append(f"[bold]Bullet diff:[/bold] {diff_text}")
                except OSError:
                    pass
                break

        # --- hm-snippet.md ---
        hm_path = apply_state_dir / "hm-snippet.md"
        if hm_path.exists():
            try:
                hm_text = hm_path.read_text().strip()
                if hm_text:
                    rows.append(f"[bold]HM:[/bold] {hm_text}")
            except OSError:
                pass

        # Nothing to show
        if not tables and not rows:
            return

        # Render tables
        for tbl in tables:
            self.console.print(tbl)

        # Render text rows
        for row in rows:
            self.console.print(row)

    def pause_before_confirm(self) -> None:
        """Stop spinner before an interactive confirmation prompt."""
        self.stop_phase()

    def print_complete(self) -> None:
        """Print the top-level apply-complete message."""
        self.console.print("\n[bold green]jobsmith apply complete.[/bold green]")

    def print_error(self, message: str) -> None:
        """Print a top-level error message."""
        self.console.print(f"[red]{message}[/red]")

    def print_info(self, message: str) -> None:
        """Print an informational message."""
        self.console.print(f"[dim]{message}[/dim]")

    # ------------------------------------------------------------------
    # Resume UX
    # ------------------------------------------------------------------

    def print_phase_skipped(self, phase_num: int, phase_name: str) -> None:
        """Render a green-checkmark panel for a phase skipped via manifest resume."""
        self.console.print(
            Panel(
                f"[bold]✓ Phase {phase_num} ({phase_name}) — already complete, skipping[/bold]",
                style="green",
                expand=False,
            )
        )

    def print_resume_banner(
        self, slug: str, phase_num: int, phase_name: str
    ) -> None:
        """Print a single-line banner above the first phase that will run."""
        self.console.print(
            f"[dim]Resuming application {slug!r} from phase {phase_num} ({phase_name})[/dim]"
        )

    def print_already_complete(self, app_dir: Path) -> None:
        """Render the all-phases-done panel; tells user to pass --force to redo."""
        self.console.print(
            Panel(
                f"[bold]Application already complete at {app_dir}.[/bold]\n"
                "Re-run with [bold]--force[/bold] to start over.",
                style="green",
                expand=False,
            )
        )

    def print_force_banner(self) -> None:
        """Yellow panel announcing that --force is bypassing prior state."""
        self.console.print(
            Panel(
                "[bold]--force: ignoring prior state[/bold]",
                style="yellow",
                expand=False,
            )
        )
