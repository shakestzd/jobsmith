"""Tests for wheel UI bundling via hatch build hook (feat-d58d5881 / slice-2).

Coverage
--------
TestWheelContainsUI
    Build the wheel with `uv build --wheel` and assert jobsmith/web_dist/index.html
    is present inside the .whl (zip) file.  Skipped when node/npm is unavailable in
    the test environment — the assertion only makes sense when vite can run.

TestLocatorFindsBundled
    Simulate the installed layout (a tempdir acting as <site-packages>/jobsmith/ with
    web_dist/index.html present) and verify slice-1's find_web_dist() resolves it as
    the bundled path.  Pure-Python, no node needed.

TestBuildHookSkipsWithoutNode
    Monkeypatch shutil.which so npm is absent; call the hook's build/copy logic
    directly and assert it logs a skip instead of raising.

TestDoctorReportsUiBundled
    Call check_ui_bundled() with both a present and absent web_dist; verify the
    returned CheckResult reflects each state correctly.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent


def _npm_available() -> bool:
    return shutil.which("npm") is not None and shutil.which("node") is not None


# ---------------------------------------------------------------------------
# TestWheelContainsUI
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _npm_available(),
    reason="node/npm not available in this environment — wheel build UI inclusion cannot be verified",
)
class TestWheelContainsUI:
    def test_wheel_contains_web_dist_index(self, tmp_path: Path) -> None:
        """Build wheel and assert jobsmith/web_dist/index.html is inside the .whl."""
        uv_bin = shutil.which("uv") or "uv"
        result = subprocess.run(
            [uv_bin, "build", "--wheel", "--out-dir", str(tmp_path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, (
            f"Wheel build failed.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        wheels = list(tmp_path.glob("*.whl"))
        assert wheels, "No .whl file produced in output dir"
        wheel = wheels[0]

        with zipfile.ZipFile(wheel) as zf:
            names = zf.namelist()

        web_dist_entries = [n for n in names if "web_dist" in n]
        assert any(
            n.endswith("web_dist/index.html") for n in web_dist_entries
        ), (
            f"jobsmith/web_dist/index.html not found in wheel.\n"
            f"web_dist entries: {web_dist_entries}\nAll entries (first 40): {names[:40]}"
        )

    def test_wheel_contains_hashed_assets(self, tmp_path: Path) -> None:
        """Build wheel and assert jobsmith/web_dist/assets/ has at least one file."""
        uv_bin = shutil.which("uv") or "uv"
        result = subprocess.run(
            [uv_bin, "build", "--wheel", "--out-dir", str(tmp_path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0

        wheels = list(tmp_path.glob("*.whl"))
        assert wheels
        wheel = wheels[0]

        with zipfile.ZipFile(wheel) as zf:
            names = zf.namelist()

        asset_entries = [n for n in names if "web_dist/assets/" in n and not n.endswith("/")]
        assert asset_entries, (
            "No hashed assets found under web_dist/assets/ in the wheel.\n"
            f"All entries (first 40): {names[:40]}"
        )


# ---------------------------------------------------------------------------
# TestLocatorFindsBundled
# ---------------------------------------------------------------------------


class TestLocatorFindsBundled:
    def test_locator_prefers_bundled_over_repo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """find_web_dist() returns the bundled path when it exists, even if repo/web/dist also exists."""
        import jobsmith.api.staticui as staticui_mod

        # Simulate installed layout: <pkg_root>/web_dist/index.html
        fake_pkg_root = tmp_path / "jobsmith"
        fake_pkg_root.mkdir()
        bundled = fake_pkg_root / "web_dist"
        bundled.mkdir()
        (bundled / "index.html").write_text("<html/>", encoding="utf-8")
        (bundled / "assets").mkdir()

        # Also create a fake repo web/dist to ensure bundled wins
        fake_repo = tmp_path
        fake_web_dist = fake_repo / "web" / "dist"
        fake_web_dist.mkdir(parents=True)
        (fake_web_dist / "index.html").write_text("<repo/>", encoding="utf-8")

        # Monkeypatch __file__ on the staticui module so package_root resolves to fake_pkg_root
        fake_staticui_file = str(fake_pkg_root / "api" / "staticui.py")
        monkeypatch.setattr(staticui_mod, "__file__", fake_staticui_file)

        result = staticui_mod.find_web_dist()
        assert result is not None, "find_web_dist() returned None but bundled path exists"
        assert result == bundled, f"Expected bundled path {bundled}, got {result}"

    def test_locator_returns_none_when_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """find_web_dist() returns None when neither bundled nor repo web/dist exist."""
        import jobsmith.api.staticui as staticui_mod

        fake_pkg_root = tmp_path / "jobsmith"
        fake_pkg_root.mkdir()

        fake_staticui_file = str(fake_pkg_root / "api" / "staticui.py")
        monkeypatch.setattr(staticui_mod, "__file__", fake_staticui_file)

        result = staticui_mod.find_web_dist()
        assert result is None, f"Expected None but got {result}"

    def test_locator_bundled_path_layout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify the bundled path layout: package_root / 'web_dist' / 'index.html'."""
        import jobsmith.api.staticui as staticui_mod

        # Layout: <jobsmith_pkg>/ web_dist/ index.html
        #                                   assets/
        fake_pkg_root = tmp_path / "jobsmith"
        (fake_pkg_root / "api").mkdir(parents=True)
        bundled = fake_pkg_root / "web_dist"
        bundled.mkdir()
        (bundled / "index.html").write_text("<html/>", encoding="utf-8")
        assets = bundled / "assets"
        assets.mkdir()
        (assets / "main-abc.js").write_text("js", encoding="utf-8")

        monkeypatch.setattr(staticui_mod, "__file__", str(fake_pkg_root / "api" / "staticui.py"))

        result = staticui_mod.find_web_dist()
        assert result == bundled
        assert (result / "index.html").exists()
        assert (result / "assets" / "main-abc.js").exists()


# ---------------------------------------------------------------------------
# TestBuildHookSkipsWithoutNode
# ---------------------------------------------------------------------------


class TestBuildHookSkipsWithoutNode:
    def test_hook_no_ops_when_npm_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When npm/node is absent, build_hook_copy_web_dist() logs a skip and does not raise."""
        # Ensure npm is not found
        monkeypatch.setattr(shutil, "which", lambda cmd: None)

        from hatch_build import build_hook_copy_web_dist

        with caplog.at_level(logging.WARNING):
            # Should not raise — no-op when npm missing
            build_hook_copy_web_dist(root=tmp_path)

        log_text = caplog.text.lower()
        assert "skip" in log_text or "unavailable" in log_text or "not found" in log_text, (
            f"Expected a skip/unavailable log message, got: {caplog.text}"
        )

    def test_hook_no_ops_leaves_no_web_dist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When npm absent, no src/jobsmith/web_dist/ is created/modified."""
        monkeypatch.setattr(shutil, "which", lambda cmd: None)

        from hatch_build import build_hook_copy_web_dist

        build_hook_copy_web_dist(root=tmp_path)

        # web_dist should NOT have been created under the fake root
        candidate = tmp_path / "src" / "jobsmith" / "web_dist"
        assert not candidate.exists(), (
            "web_dist should not be created when npm is absent"
        )

    def test_hook_removes_stale_web_dist_when_npm_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pre-existing (stale) web_dist is purged even when the build is skipped.

        Otherwise an API-only skip would silently ship a stale UI bundle from a
        previous build (roborev-991 MEDIUM).
        """
        monkeypatch.setattr(shutil, "which", lambda cmd: None)

        stale = tmp_path / "src" / "jobsmith" / "web_dist"
        stale.mkdir(parents=True)
        (stale / "index.html").write_text("<stale/>", encoding="utf-8")

        from hatch_build import build_hook_copy_web_dist

        build_hook_copy_web_dist(root=tmp_path)

        assert not stale.exists(), (
            "stale web_dist must be removed before skipping the build"
        )


# ---------------------------------------------------------------------------
# TestDoctorReportsUiBundled
# ---------------------------------------------------------------------------


class TestDoctorReportsUiBundled:
    def test_check_ui_bundled_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """check_ui_bundled() returns ok=True when find_web_dist resolves to a real path."""
        import jobsmith.doctor as doctor_mod

        bundled = tmp_path / "web_dist"
        bundled.mkdir()
        (bundled / "index.html").write_text("<html/>", encoding="utf-8")

        monkeypatch.setattr(
            "jobsmith.doctor.find_web_dist",
            lambda: bundled,
        )

        result = doctor_mod.check_ui_bundled()
        assert result.ok is True, f"Expected ok=True, got: {result}"
        assert "yes" in result.message.lower() or "bundled" in result.message.lower(), (
            f"Expected 'yes'/'bundled' in message, got: {result.message}"
        )

    def test_check_ui_bundled_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """check_ui_bundled() returns ok=False when find_web_dist returns None."""
        import jobsmith.doctor as doctor_mod

        monkeypatch.setattr(
            "jobsmith.doctor.find_web_dist",
            lambda: None,
        )

        result = doctor_mod.check_ui_bundled()
        assert result.ok is False, f"Expected ok=False, got: {result}"
        assert "no" in result.message.lower() or "absent" in result.message.lower() or "missing" in result.message.lower(), (
            f"Expected 'no'/'absent'/'missing' in message, got: {result.message}"
        )

    def test_check_ui_bundled_included_in_run_all_checks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_all_checks() includes the ui_bundled check."""
        import jobsmith.doctor as doctor_mod

        monkeypatch.setattr("jobsmith.doctor.find_web_dist", lambda: None)

        results = doctor_mod.run_all_checks()
        names = [r.name for r in results]
        assert "ui_bundled" in names, (
            f"'ui_bundled' check not found in run_all_checks() results. Names: {names}"
        )
