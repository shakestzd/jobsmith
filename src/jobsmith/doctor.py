"""jobsmith doctor — preflight environment checks.

Each check returns a CheckResult. The ``preflight()`` function runs all checks,
prints pass/fail to stderr, and returns True iff all checks pass.

Callers that want programmatic access (e.g. a future ``jobsmith apply`` guard)
should import and call ``preflight()`` directly.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
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


def check_claude_auth() -> CheckResult:
    """Verify Claude authentication via OAuth/keychain or ANTHROPIC_API_KEY.

    1. Runs ``claude auth status`` (JSON output). If ``loggedIn == true``, PASS.
    2. Falls back to ANTHROPIC_API_KEY env var (strips whitespace per F6 nit).
    3. Otherwise FAIL with remediation for both auth paths.
    """
    try:
        proc = subprocess.run(
            ["claude", "auth", "status", "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        data = json.loads(proc.stdout)
        if data.get("loggedIn") is True:
            email = data.get("email", "unknown")
            plan = data.get("subscriptionType", "unknown")
            return CheckResult(
                name="claude_auth",
                ok=True,
                message=f"authenticated as {email} ({plan})",
            )
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        pass

    # Fallback: API key (strip whitespace so "   " is treated as not set)
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return CheckResult(
            name="claude_auth",
            ok=True,
            message="ANTHROPIC_API_KEY set",
        )

    return CheckResult(
        name="claude_auth",
        ok=False,
        message="no Claude authentication found",
        remediation=(
            "run 'claude /login' (Max/Pro subscription) "
            "or export ANTHROPIC_API_KEY=sk-..."
        ),
    )


# Keep old name as an alias so external callers aren't immediately broken.
def check_anthropic_api_key() -> CheckResult:
    """Deprecated alias for check_claude_auth(); kept for backwards compatibility."""
    return check_claude_auth()


def check_apply_config(cwd: Optional[Path] = None) -> CheckResult:
    """Verify ``.apply-config.yaml`` is reachable from the working directory.

    Walks up the directory tree like ``jobsmith.config.find_config`` so that
    invocation from a project subdirectory still passes.
    """
    from .config import find_config

    directory = cwd or Path.cwd()
    config_path = find_config(directory)
    if config_path is not None:
        return CheckResult(
            name="apply_config",
            ok=True,
            message=f".apply-config.yaml found at {config_path}",
        )
    return CheckResult(
        name="apply_config",
        ok=False,
        message=f".apply-config.yaml not found from {directory} (walked up to filesystem root)",
        remediation="run `jobsmith init` in the project root",
    )


def check_master_yaml(cwd: Optional[Path] = None) -> CheckResult:
    """Verify every master YAML file declared by ``.apply-config.yaml`` exists.

    Resolves the config via ``find_config`` (subdirectory-aware), loads it,
    and validates every path returned by ``all_master_paths(config, repo_root)``.
    Falls back to a plain ``work.yml`` lookup in ``cwd`` when no config is
    discoverable so that callers without a configured project still get a
    sensible diagnostic.
    """
    from .config import find_config, load_config
    from .paths import all_master_paths

    directory = cwd or Path.cwd()
    config_path = find_config(directory)

    if config_path is None:
        # No config — fall back to a plain ``work.yml`` lookup so users in a
        # bare directory get a useful message rather than a config error.
        candidate = directory / "work.yml"
        if candidate.is_file():
            return CheckResult(
                name="master_yaml",
                ok=True,
                message=f"master work YAML found at {candidate} (no .apply-config.yaml present)",
            )
        return CheckResult(
            name="master_yaml",
            ok=False,
            message=f"no .apply-config.yaml found from {directory}; cannot determine master YAML paths",
            remediation="run `jobsmith init` to scaffold the project layout",
        )

    try:
        config = load_config(config_path)
    except Exception as exc:
        return CheckResult(
            name="master_yaml",
            ok=False,
            message=f"failed to load {config_path}: {exc}",
            remediation="run `jobsmith validate` to see the underlying config error",
        )

    repo_root = config_path.parent
    expected = all_master_paths(config, repo_root)
    missing = [p for p in expected if not p.exists()]
    if not missing:
        return CheckResult(
            name="master_yaml",
            ok=True,
            message=f"all {len(expected)} master YAML files present (relative to {repo_root})",
        )
    rendered = ", ".join(str(p) for p in missing)
    return CheckResult(
        name="master_yaml",
        ok=False,
        message=f"missing master YAML file(s): {rendered}",
        remediation=(
            "create the missing files, or run `jobsmith init` to scaffold from examples"
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
        check_claude_auth(),
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
    "check_anthropic_api_key",  # deprecated alias
    "check_claude_auth",
    "check_apply_config",
    "check_claude_binary",
    "check_master_yaml",
    "check_plugin_dir_resolves",
    "check_python_version",
    "preflight",
    "run_all_checks",
]
