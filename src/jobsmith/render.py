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

import sys

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from . import headless

# Max chars for truncated args / result previews
_MAX_ARG_CHARS = 80
_MAX_RESULT_CHARS = 100
_MAX_SPINNER_CHARS = 60

# Number of key=value pairs to show in tool args
_MAX_KV_PAIRS = 3

# Priority keys to look at first when summarising tool_input
_PRIORITY_KEYS = ("command", "path", "url", "query", "input")


def _format_tool_args(tool_input: dict | None, max_chars: int) -> str:
    """Summarise *tool_input* as ``key=value, …`` truncated to *max_chars*."""
    if not tool_input:
        return ""

    pairs: list[str] = []
    remaining_keys = list(tool_input.keys())

    # Priority keys first
    for key in _PRIORITY_KEYS:
        if key in tool_input:
            val = str(tool_input[key])
            # Truncate individual value to keep things readable
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
    one_line = result.replace("\n", " ").strip()
    if len(one_line) > max_chars:
        return one_line[: max_chars - 1] + "…"
    return one_line


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
            args = _format_tool_args(event.tool_input, max_chars=width - 12)
            name = event.tool_name or "?"
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
                f"[cyan]→[/cyan] [bold]{name}[/bold]([dim]{args}[/dim])"
            )

        elif event.type == "tool_result":
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
