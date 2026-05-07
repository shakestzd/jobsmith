"""Tests for Slice 5 — cli.py:apply is a shim."""
import inspect
import jobsmith.cli as cli_mod


def test_cli_apply_command_exists():
    """The Typer apply command is still defined."""
    assert hasattr(cli_mod, "apply") or hasattr(cli_mod, "apply_cmd"), (
        "cli.py must still define an apply command"
    )


def test_cli_apply_is_short():
    """cli.py:apply should be a thin shim — under ~50 lines of body."""
    fn = getattr(cli_mod, "apply", None) or getattr(cli_mod, "apply_cmd", None)
    src = inspect.getsource(fn)
    line_count = sum(1 for line in src.splitlines() if line.strip() and not line.strip().startswith("#"))
    assert line_count < 60, f"apply command body too long ({line_count} lines) — Slice 5 wants a shim"


def test_cli_apply_imports_from_core():
    """The new shim should import from jobsmith.core.pipeline."""
    src = inspect.getsource(cli_mod)
    # Either direct import OR delegation to apply.run_apply (which itself uses core)
    assert (
        "jobsmith.core.pipeline" in src
        or "from jobsmith.apply import run_apply" in src
        or "from .apply import run_apply" in src
    ), "cli.py should consume core.pipeline (directly or via apply.run_apply)"
