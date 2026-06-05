"""Custom hatchling build hook — bundle the Vite web app into the wheel.

At wheel-build time this hook:
  1. Runs ``vite build`` (via ``npm run build``) inside ``web/`` FRESH — never trusts
     a stale on-disk ``web/dist``.
  2. Copies the built output into ``src/jobsmith/web_dist/`` so the wheel's
     force-include rule can pick it up.

If ``npm`` or ``node`` is absent the hook logs a WARNING and returns without raising —
the wheel still builds successfully in API-only mode (no UI bundled).

This hook is intentionally a NO-OP for editable installs (``pip install -e .``);
in that case developers should run ``npm run build`` in ``web/`` manually or use
``jobsmith up --dev`` (documented in slice-5 of plan-72ad5ccc).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path

# hatchling is only available in a build environment, not at test/runtime.
# The BuildHookInterface import is deferred to the class body so that
# ``import hatch_build`` works in tests without hatchling installed.
_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public helper — importable by tests without hatchling machinery
# ---------------------------------------------------------------------------


def build_hook_copy_web_dist(root: Path) -> None:
    """Run vite build and copy output to src/jobsmith/web_dist/.

    Parameters
    ----------
    root:
        The project root directory (same as ``self.root`` in hatchling hooks).

    Behaviour
    ---------
    - Returns immediately (logs WARNING) when npm or node are not on PATH.
    - Raises ``subprocess.CalledProcessError`` on npm build failure so the wheel
      build fails loudly rather than silently shipping an empty UI.
    """
    if shutil.which("npm") is None or shutil.which("node") is None:
        _log.warning(
            "jobsmith build hook: npm/node not found — skipping vite build. "
            "The wheel will be built in API-only mode (no UI bundled). "
            "Install node + npm if you want the UI included."
        )
        return

    web_dir = root / "web"
    if not web_dir.is_dir():
        _log.warning(
            "jobsmith build hook: web/ directory not found at %s — skipping vite build.",
            web_dir,
        )
        return

    _log.info("jobsmith build hook: running `npm run build` in %s …", web_dir)
    subprocess.run(
        ["npm", "run", "build"],
        cwd=str(web_dir),
        check=True,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )

    src_dist = web_dir / "dist"
    if not src_dist.is_dir():
        raise RuntimeError(
            f"jobsmith build hook: vite build succeeded but {src_dist} does not exist."
        )

    dest = root / "src" / "jobsmith" / "web_dist"

    # Always start fresh — remove any stale copy from a previous build.
    if dest.exists():
        shutil.rmtree(dest)

    shutil.copytree(src_dist, dest)
    _log.info("jobsmith build hook: copied %s → %s", src_dist, dest)


# ---------------------------------------------------------------------------
# Hatchling integration
# ---------------------------------------------------------------------------
# hatchling is only present in a build environment.  We define the hook class
# only when hatchling is importable so that `import hatch_build` in tests
# (where hatchling is absent) works without error.

try:
    from hatchling.builders.hooks.plugin.interface import BuildHookInterface as _BuildHookInterface

    class CustomBuildHook(_BuildHookInterface):
        """Hatchling custom build hook that bundles the Vite UI into the wheel."""

        PLUGIN_NAME = "custom"

        def initialize(self, version: str, build_data: dict) -> None:  # noqa: ARG002
            """Called before the build; run vite and copy output into the package tree."""
            build_hook_copy_web_dist(root=Path(self.root))

except ModuleNotFoundError:
    # Running outside a build environment (e.g. test suite without hatchling).
    # Nothing to define here — the hook class is only needed by hatchling itself.
    pass
