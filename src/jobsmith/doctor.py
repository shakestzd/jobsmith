"""jobsmith doctor — preflight environment checks.

Each check returns a CheckResult. The ``preflight()`` function runs all checks,
prints pass/fail to stderr, and returns True iff all checks pass.

Callers that want programmatic access (e.g. a future ``jobsmith apply`` guard)
should import and call ``preflight()`` directly.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


@dataclass
class CheckResult:
    name: str
    ok: bool
    message: str          # short pass/fail explanation
    remediation: Optional[str] = None   # shown on fail


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_claude_binary() -> CheckResult:
    """Verify ``claude`` (Claude Code CLI) is on PATH."""
    path = shutil.which("claude")
    if path:
        return CheckResult(
            name="claude_binary",
            ok=True,
            message=f"claude found at {path}",
        )
    return CheckResult(
        name="claude_binary",
        ok=False,
        message="claude not found on PATH",
        remediation=(
            "Install Claude Code: npm install -g @anthropic-ai/claude-code  "
            "or visit https://docs.anthropic.com/en/docs/claude-code"
        ),
    )


def check_anthropic_api_key() -> CheckResult:
    """Verify ANTHROPIC_API_KEY environment variable is set and non-empty."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return CheckResult(
            name="anthropic_api_key",
            ok=True,
            message="ANTHROPIC_API_KEY is set",
        )
    return CheckResult(
        name="anthropic_api_key",
        ok=False,
        message="ANTHROPIC_API_KEY is not set",
        remediation="export ANTHROPIC_API_KEY=sk-...",
    )


def check_apply_config(cwd: Optional[Path] = None) -> CheckResult:
    """Verify ``.apply-config.yaml`` exists in the working directory."""
    directory = cwd or Path.cwd()
    config_path = directory / ".apply-config.yaml"
    if config_path.is_file():
        return CheckResult(
            name="apply_config",
            ok=True,
            message=f".apply-config.yaml found at {config_path}",
        )
    return CheckResult(
        name="apply_config",
        ok=False,
        message=f".apply-config.yaml not found in {directory}",
        remediation="run `jobsmith init` in this directory",
    )


# Accepted master work YAML filenames (in order of preference).
_MASTER_WORK_FILENAMES = ("work.yml", "work.yaml", ".work.yaml")


def check_master_yaml(cwd: Optional[Path] = None) -> CheckResult:
    """Verify at least one master work YAML file exists in the working directory.

    Looks for: work.yml, work.yaml, .work.yaml  (in that order).
    The canonical filename used by ``jobsmith init`` is ``work.yml``
    (via ``assets/content/work.yml``); the alternatives provide grace for
    users who placed their file at the repo root under alternate names.
    """
    directory = cwd or Path.cwd()
    for filename in _MASTER_WORK_FILENAMES:
        candidate = directory / filename
        if candidate.is_file():
            return CheckResult(
                name="master_yaml",
                ok=True,
                message=f"master work YAML found at {candidate}",
            )
    checked = ", ".join(_MASTER_WORK_FILENAMES)
    return CheckResult(
        name="master_yaml",
        ok=False,
        message=f"no master work YAML found in {directory} (checked: {checked})",
        remediation=(
            "create work.yml (or work.yaml) in the working directory, "
            "or run `jobsmith init` to scaffold the full repo layout"
        ),
    )


def check_python_version(min_major: int = 3, min_minor: int = 10) -> CheckResult:
    """Verify the running Python interpreter meets the minimum version requirement."""
    vi = sys.version_info
    if vi >= (min_major, min_minor):
        return CheckResult(
            name="python_version",
            ok=True,
            message=f"Python {vi.major}.{vi.minor}.{vi.micro} >= {min_major}.{min_minor}",
        )
    return CheckResult(
        name="python_version",
        ok=False,
        message=(
            f"Python {vi.major}.{vi.minor}.{vi.micro} is below "
            f"the required {min_major}.{min_minor}"
        ),
        remediation=f"upgrade Python to {min_major}.{min_minor}+",
    )


def check_plugin_dir_resolves() -> CheckResult:
    """Verify jobsmith's embedded plugin directory is present and valid."""
    import jobsmith

    try:
        pdir = jobsmith.plugin_dir()
    except Exception as exc:
        return CheckResult(
            name="plugin_dir",
            ok=False,
            message=f"jobsmith.plugin_dir() raised: {exc}",
            remediation="reinstall the jobsmith package: uv pip install --upgrade jobsmith",
        )

    plugin_json = pdir / "plugin.json"
    if pdir.exists() and plugin_json.is_file():
        return CheckResult(
            name="plugin_dir",
            ok=True,
            message=f"plugin dir found at {pdir}",
        )

    if not pdir.exists():
        detail = f"directory does not exist: {pdir}"
    else:
        detail = f"plugin.json missing from {pdir}"

    return CheckResult(
        name="plugin_dir",
        ok=False,
        message=detail,
        remediation="reinstall the jobsmith package: uv pip install --upgrade jobsmith",
    )


# ---------------------------------------------------------------------------
# Aggregate helpers
# ---------------------------------------------------------------------------

def run_all_checks(cwd: Optional[Path] = None) -> list[CheckResult]:
    """Run every preflight check and return results in a stable order."""
    return [
        check_python_version(),
        check_claude_binary(),
        check_anthropic_api_key(),
        check_apply_config(cwd),
        check_master_yaml(cwd),
        check_plugin_dir_resolves(),
    ]


def preflight(cwd: Optional[Path] = None) -> bool:
    """Run all checks; return True if all pass, False otherwise.

    Prints per-check status to stderr in the format::

        [PASS] <name>: <message>
        [FAIL] <name>: <message>
                → <remediation>
    """
    results = run_all_checks(cwd)
    for result in results:
        if result.ok:
            print(f"[PASS] {result.name}: {result.message}", file=sys.stderr)
        else:
            print(f"[FAIL] {result.name}: {result.message}", file=sys.stderr)
            if result.remediation:
                print(f"        → {result.remediation}", file=sys.stderr)
    return all(r.ok for r in results)


__all__ = [
    "CheckResult",
    "check_anthropic_api_key",
    "check_apply_config",
    "check_claude_binary",
    "check_master_yaml",
    "check_plugin_dir_resolves",
    "check_python_version",
    "preflight",
    "run_all_checks",
]
