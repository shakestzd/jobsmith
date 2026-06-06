"""Tests for jobsmith.core.pipeline.core_run_apply — Slice 3c."""
import inspect

from jobsmith import apply as apply_mod
from jobsmith.core import pipeline as core_pipeline


def test_core_run_apply_importable():
    """core.pipeline must expose core_run_apply (or run_apply)."""
    fn = getattr(core_pipeline, "core_run_apply", None) or getattr(core_pipeline, "run_apply", None)
    assert callable(fn), "core.pipeline.core_run_apply (or run_apply) missing"


def test_core_run_apply_accepts_event_sink():
    """The core run_apply variant takes an events sink — no Rich coupling."""
    fn = getattr(core_pipeline, "core_run_apply", None) or getattr(core_pipeline, "run_apply", None)
    sig = inspect.signature(fn)
    params = list(sig.parameters.keys())
    assert "events" in params or "sink" in params or "rdr" in params, f"params={params}"


def test_apply_run_apply_still_callable():
    """jobsmith.apply.run_apply remains the public CLI entry point."""
    assert callable(apply_mod.run_apply)


def test_run_apply_signature_unchanged_for_callers():
    """The CLI-facing run_apply signature must accept its existing kwargs."""
    sig = inspect.signature(apply_mod.run_apply)
    params = sig.parameters
    assert "url" in params
    assert "cwd" in params or any(
        p.default is not inspect.Parameter.empty for p in params.values()
    )


def test_db_now_iso_importable_from_core():
    """_db_now_iso utility must live in core.pipeline."""
    fn = getattr(core_pipeline, "_db_now_iso", None)
    assert callable(fn), "_db_now_iso not found in core.pipeline"


def test_db_now_iso_returns_iso_string():
    """_db_now_iso must return a non-empty ISO-8601 string."""
    result = core_pipeline._db_now_iso()
    assert isinstance(result, str)
    assert "T" in result  # ISO-8601 separator


def test_open_pipeline_db_for_run_importable_from_core():
    """_open_pipeline_db_for_run utility must live in core.pipeline."""
    fn = getattr(core_pipeline, "_open_pipeline_db_for_run", None)
    assert callable(fn), "_open_pipeline_db_for_run not found in core.pipeline"


def test_open_pipeline_db_for_run_returns_none_when_no_config(tmp_path):
    """_open_pipeline_db_for_run returns None gracefully when config is absent."""
    result = core_pipeline._open_pipeline_db_for_run(tmp_path)
    assert result is None


def test_core_run_apply_accepts_url_param():
    """core_run_apply (or run_apply in core) must accept a url parameter."""
    fn = getattr(core_pipeline, "core_run_apply", None) or getattr(core_pipeline, "run_apply", None)
    sig = inspect.signature(fn)
    assert "url" in sig.parameters, f"url not in {list(sig.parameters.keys())}"


def test_core_pipeline_no_rich_import():
    """core.pipeline module must not import rich, click, or typer at module level."""
    import sys
    # Re-import to check module-level state (already imported)
    mod = sys.modules.get("jobsmith.core.pipeline")
    assert mod is not None
    # Check the module's source doesn't have top-level rich/click/typer imports
    import inspect as _inspect
    src = _inspect.getsource(mod)
    # Tolerate inline imports inside function bodies — but no top-level import of rich/click/typer
    lines = src.splitlines()
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        # Only flag module-level imports (no leading whitespace); inline imports
        # inside function bodies are tolerated.
        module_level = not line.startswith((" ", "\t"))
        if module_level and (stripped.startswith("import rich") or stripped.startswith("from rich")):
            raise AssertionError(f"core.pipeline imports rich at module level (line {i}): {line!r}")
        if module_level and (stripped.startswith("import click") or stripped.startswith("from click")):
            raise AssertionError(f"core.pipeline imports click at module level (line {i}): {line!r}")
        if module_level and (stripped.startswith("import typer") or stripped.startswith("from typer")):
            raise AssertionError(f"core.pipeline imports typer at module level (line {i}): {line!r}")
