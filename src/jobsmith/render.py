"""render.py — rich-rendered terminal output helpers for `jobsmith apply`.

Provides :class:`ApplyRenderer` which encapsulates all rich Console/Progress
interactions for the three-phase apply pipeline.

Design goals
------------
- Phase headers as cyan ``rich.panel.Panel``
- Per-phase ``rich.progress.Progress`` spinner (skipped in --yes / non-TTY modes)
- Tool calls as ``[cyan]→[/] [bold]Tool[/]([dim]args[/])``
- Tool results as ``[dim]← result…[/]``
- Phase complete / failed as styled summary panels
- Non-TTY fallback: plain line-by-line output, no spinners
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

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
    console:
        Optionally supply a pre-built Console (useful for tests).  When None,
        one is constructed automatically with TTY-aware defaults.
    """

    def __init__(
        self,
        *,
        yes: bool = False,
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
        self._use_spinner = (not yes) and self.console.is_terminal
        self._progress: Progress | None = None
        # Track tool_use_ids for filtered tools so we can drop their tool_results
        self._filtered_tool_use_ids: set[str] = set()
        # Track whether we're currently inside an Agent dispatch
        self._inside_agent: bool = False

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def print_header(self, phase_num: int, total: int, phase_name: str) -> None:
        """Render the phase header panel."""
        title = f"Phase {phase_num} / {total} — {phase_name.capitalize()}"
        self.console.print(Panel(f"[bold]{title}[/bold]", style="cyan", expand=False))

    def start_phase(self, phase_name: str) -> None:
        """Start the spinner for a new phase (no-op in non-spinner mode)."""
        if not self._use_spinner:
            return
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=self.console,
            transient=False,
        )
        self._progress.start()
        self._progress.add_task(description=f"Running {phase_name}…", total=None)

    def stop_phase(self) -> None:
        """Stop the spinner (no-op if not running)."""
        if self._progress is not None:
            self._progress.stop()
            self._progress = None

    def render_event(self, event: headless.Event) -> None:
        """Render a single event to the console."""
        width = self.console.width or 120

        if event.type == "tool_use":
            name = event.tool_name or "?"

            # Filter: skip TodoWrite and ToolSearch entirely
            if name in _FILTERED_TOOLS:
                tool_use_id = _extract_tool_use_id(event)
                if tool_use_id:
                    self._filtered_tool_use_ids.add(tool_use_id)
                return

            args = _format_tool_args(event.tool_input, max_chars=width - 12)

            # Agent dispatch: use nesting prefix
            if name == "Agent":
                self._inside_agent = True
                indent = "│ "
            else:
                if self._inside_agent:
                    self._inside_agent = False
                indent = ""

            # Update spinner description when active
            if self._progress is not None:
                tasks = self._progress.tasks
                if tasks:
                    desc = f"[cyan]→[/cyan] {name}…"
                    # Truncate to _MAX_SPINNER_CHARS
                    if len(desc) > _MAX_SPINNER_CHARS:
                        desc = desc[: _MAX_SPINNER_CHARS - 1] + "…"
                    self._progress.update(tasks[0].id, description=desc)

            # Print below spinner
            self.console.print(
                f"{indent}[cyan]→[/cyan] [bold]{name}[/bold]([dim]{args}[/dim])"
            )

        elif event.type == "tool_result":
            # Filter: drop results whose tool_use_id was filtered
            tool_use_id = event.tool_name  # headless.py stores tool_use_id in tool_name
            if tool_use_id and tool_use_id in self._filtered_tool_use_ids:
                return

            preview = _format_result(event.tool_result, max_chars=_MAX_RESULT_CHARS)
            self.console.print(f"[dim]← {preview}[/dim]")

        elif event.type == "text" and event.text:
            stripped = event.text.strip()
            if stripped:
                self.console.print(f"[dim italic]{stripped}[/dim italic]")

        elif event.type == "error":
            self.stop_phase()
            self.console.print(f"[red]✗ {event.error}[/red]")

        elif event.type == "phase_complete":
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
