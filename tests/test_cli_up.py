"""Tests for `jobsmith up` command (feat-2423bbec).

TDD: tests are written first and must fail before the implementation is added.
No real server/port is bound or browser opened during these tests.
"""
from __future__ import annotations

import logging
import os
import threading
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from jobsmith.cli import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helper: build a mock for uvicorn.run that records calls and blocks briefly
# ---------------------------------------------------------------------------


def _make_uvicorn_mock():
    """Return a MagicMock that acts as uvicorn.run (returns immediately)."""
    return MagicMock(return_value=None)


# ---------------------------------------------------------------------------
# TestUpOpensBrowserAfterListen
# ---------------------------------------------------------------------------


class TestUpOpensBrowserAfterListen:
    """Daemon thread starts BEFORE uvicorn.run, then opens browser after port ready."""

    def test_no_open_flag_suppresses_browser(self):
        """--no-open: server runs but the browser is never opened."""
        with (
            patch("jobsmith.api.server.uvicorn") as mock_uv_mod,
            patch("jobsmith.api.server.webbrowser") as mock_wb_mod,
            patch("jobsmith.api.server._wait_for_port", MagicMock(return_value=None)),
        ):
            mock_uv_mod.run = MagicMock(return_value=None)

            result = runner.invoke(app, ["up", "--no-open"])
            assert result.exit_code == 0, result.output
            mock_wb_mod.open.assert_not_called()

    def test_browser_not_opened_with_no_open_flag(self):
        """--no-open suppresses webbrowser.open entirely."""
        mock_uvicorn = MagicMock(return_value=None)
        mock_wait = MagicMock(return_value=None)

        with (
            patch("jobsmith.api.server.uvicorn") as mock_uv_mod,
            patch("jobsmith.api.server.webbrowser") as mock_wb_mod,
            patch("jobsmith.api.server._wait_for_port", mock_wait),
        ):
            mock_uv_mod.run = mock_uvicorn

            result = runner.invoke(app, ["up", "--no-open"])
            assert result.exit_code == 0, result.output
            mock_wb_mod.open.assert_not_called()

    def test_daemon_thread_started_before_uvicorn(self):
        """Daemon thread (browser-opener) is started before uvicorn.run is called."""
        call_order: list[str] = []
        threads_before_uvicorn: list[int] = []

        original_thread_start = threading.Thread.start

        def tracking_start(self, *a, **kw):
            call_order.append(f"thread_start:{self.name}")
            original_thread_start(self, *a, **kw)

        def tracking_uvicorn(*a, **kw):
            # Record how many "browser" threads were started before uvicorn
            browser_threads = [x for x in call_order if "browser" in x or "open" in x]
            threads_before_uvicorn.extend(browser_threads)
            call_order.append("uvicorn.run")

        mock_wait = MagicMock(return_value=None)

        with (
            patch("jobsmith.api.server.uvicorn") as mock_uv_mod,
            patch("jobsmith.api.server.webbrowser"),
            patch("jobsmith.api.server._wait_for_port", mock_wait),
            patch.object(threading.Thread, "start", tracking_start),
        ):
            mock_uv_mod.run = tracking_uvicorn

            result = runner.invoke(app, ["up"])
            assert result.exit_code == 0, result.output

        # A thread should have been started BEFORE uvicorn.run
        uvicorn_idx = next(
            (i for i, x in enumerate(call_order) if x == "uvicorn.run"), None
        )
        thread_indices = [i for i, x in enumerate(call_order) if "thread_start" in x]
        assert uvicorn_idx is not None, "uvicorn.run was never called"
        assert any(
            t_idx < uvicorn_idx for t_idx in thread_indices
        ), f"No thread started before uvicorn.run. Order: {call_order}"

    def test_browser_opens_correct_url_default(self):
        """Browser opens http://127.0.0.1:8000 by default."""
        opened_urls: list[str] = []

        def fake_wait(host, port, *, timeout):
            pass  # "instant" ready

        def fake_open(url):
            opened_urls.append(url)

        with (
            patch("jobsmith.api.server.uvicorn") as mock_uv_mod,
            patch("jobsmith.api.server.webbrowser") as mock_wb_mod,
            patch("jobsmith.api.server._wait_for_port", fake_wait),
        ):
            mock_uv_mod.run = MagicMock(return_value=None)
            mock_wb_mod.open = fake_open

            result = runner.invoke(app, ["up"])
            assert result.exit_code == 0, result.output

        assert any("127.0.0.1:8000" in u for u in opened_urls), (
            f"Expected URL with 127.0.0.1:8000, got: {opened_urls}"
        )

    def test_browser_opens_correct_url_custom_port(self):
        """Browser opens http://127.0.0.1:<custom_port> when --port is supplied."""
        opened_urls: list[str] = []

        def fake_wait(host, port, *, timeout):
            pass

        def fake_open(url):
            opened_urls.append(url)

        with (
            patch("jobsmith.api.server.uvicorn") as mock_uv_mod,
            patch("jobsmith.api.server.webbrowser") as mock_wb_mod,
            patch("jobsmith.api.server._wait_for_port", fake_wait),
        ):
            mock_uv_mod.run = MagicMock(return_value=None)
            mock_wb_mod.open = fake_open

            result = runner.invoke(app, ["up", "--port", "9999"])
            assert result.exit_code == 0, result.output

        assert any("9999" in u for u in opened_urls), (
            f"Expected URL with port 9999, got: {opened_urls}"
        )


# ---------------------------------------------------------------------------
# TestUpPortInUse
# ---------------------------------------------------------------------------


class TestUpPortInUse:
    """Bind OSError / EADDRINUSE → friendly message, non-zero exit, no stacktrace."""

    def test_port_in_use_friendly_message(self):
        """EADDRINUSE results in a friendly message mentioning --port."""
        import errno

        err = OSError(errno.EADDRINUSE, "Address already in use")

        mock_wait = MagicMock(return_value=None)

        with (
            patch("jobsmith.api.server.uvicorn") as mock_uv_mod,
            patch("jobsmith.api.server.webbrowser"),
            patch("jobsmith.api.server._wait_for_port", mock_wait),
        ):
            mock_uv_mod.run = MagicMock(side_effect=err)

            result = runner.invoke(app, ["up"])

        assert result.exit_code != 0, "Expected non-zero exit for port-in-use"
        assert "--port" in result.output, (
            f"Expected '--port' in output. Got: {result.output!r}"
        )
        # No Python traceback
        assert "Traceback" not in result.output, (
            f"Unexpected traceback in output: {result.output!r}"
        )

    def test_port_in_use_non_zero_exit(self):
        """Exit code is non-zero when port is in use."""
        import errno

        err = OSError(errno.EADDRINUSE, "Address already in use")

        with (
            patch("jobsmith.api.server.uvicorn") as mock_uv_mod,
            patch("jobsmith.api.server.webbrowser"),
            patch("jobsmith.api.server._wait_for_port", MagicMock()),
        ):
            mock_uv_mod.run = MagicMock(side_effect=err)
            result = runner.invoke(app, ["up"])

        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# TestUpDevSkipsStaticMount
# ---------------------------------------------------------------------------


class TestUpDevSkipsStaticMount:
    """--dev sets JOBSMITH_DEV=1 so the static mount is skipped."""

    def test_dev_flag_sets_env_var(self):
        """`up --dev` sets JOBSMITH_DEV=1 before uvicorn.run (and thus create_app)."""
        captured: dict[str, str | None] = {}

        def capture_env_run(*a, **kw):
            # up_serve sets the env var BEFORE calling uvicorn.run.
            captured["dev"] = os.environ.get("JOBSMITH_DEV")

        # patch.dict snapshots os.environ and restores it on exit (no leak).
        with (
            patch.dict(os.environ),
            patch("jobsmith.api.server.uvicorn") as mock_uv_mod,
            patch("jobsmith.api.server._wait_for_port", MagicMock()),
        ):
            os.environ.pop("JOBSMITH_DEV", None)
            mock_uv_mod.run = capture_env_run
            result = runner.invoke(app, ["up", "--dev", "--no-open"])
            assert result.exit_code == 0, result.output

        assert captured.get("dev") == "1", (
            f"Expected JOBSMITH_DEV=1 at uvicorn.run time, got: {captured.get('dev')!r}"
        )

    def test_no_dev_flag_clears_env_var(self):
        """`up` (no --dev) ensures JOBSMITH_DEV is NOT '1' at uvicorn.run time."""
        captured: dict[str, str | None] = {}

        def capture_env_run(*a, **kw):
            captured["dev"] = os.environ.get("JOBSMITH_DEV")

        with (
            patch.dict(os.environ),
            patch("jobsmith.api.server.uvicorn") as mock_uv_mod,
            patch("jobsmith.api.server._wait_for_port", MagicMock()),
        ):
            # Even with a stale JOBSMITH_DEV set, prod `up` must clear it.
            os.environ["JOBSMITH_DEV"] = "1"
            mock_uv_mod.run = capture_env_run
            result = runner.invoke(app, ["up", "--no-open"])
            assert result.exit_code == 0, result.output

        assert captured.get("dev") != "1", (
            "Expected JOBSMITH_DEV cleared (not '1') when --dev is not passed"
        )

    def test_static_ui_skipped_in_dev_mode(self):
        """mount_static_ui is NOT called when JOBSMITH_DEV=1 is set."""
        with (
            patch.dict(os.environ, {"JOBSMITH_DEV": "1"}),
            patch("jobsmith.api.main.mount_static_ui") as mock_mount,
        ):
            from jobsmith.api import main as main_mod

            main_mod.create_app()
            mock_mount.assert_not_called()

    def test_static_ui_mounted_in_prod_mode(self):
        """mount_static_ui IS called when JOBSMITH_DEV is not set."""
        env_without_dev = {k: v for k, v in os.environ.items() if k != "JOBSMITH_DEV"}
        with (
            patch.dict(os.environ, env_without_dev, clear=True),
            patch("jobsmith.api.main.mount_static_ui") as mock_mount,
        ):
            from jobsmith.api import main as main_mod

            main_mod.create_app()
            mock_mount.assert_called_once()


# ---------------------------------------------------------------------------
# TestUpBindPublicNoAutoAuth
# ---------------------------------------------------------------------------


class TestUpBindPublicNoAutoAuth:
    """--bind-public → non-localhost bind host passed through."""

    def test_bind_public_uses_public_host(self):
        """--bind-public causes serve() to receive host=0.0.0.0."""
        captured_args: dict = {}

        def fake_serve(host, port, *, open_browser, dev):
            captured_args["host"] = host
            captured_args["port"] = port

        with patch("jobsmith.cli.up_serve", fake_serve):
            result = runner.invoke(app, ["up", "--bind-public", "--no-open"])
            assert result.exit_code == 0, result.output

        assert captured_args.get("host") == "0.0.0.0", (
            f"Expected host=0.0.0.0, got: {captured_args.get('host')!r}"
        )

    def test_default_host_is_localhost(self):
        """Default (no --bind-public) uses 127.0.0.1."""
        captured_args: dict = {}

        def fake_serve(host, port, *, open_browser, dev):
            captured_args["host"] = host

        with patch("jobsmith.cli.up_serve", fake_serve):
            result = runner.invoke(app, ["up", "--no-open"])
            assert result.exit_code == 0, result.output

        assert captured_args.get("host") == "127.0.0.1", (
            f"Expected host=127.0.0.1, got: {captured_args.get('host')!r}"
        )


# ---------------------------------------------------------------------------
# TestBindModeEnvGate — auto-auth gated on real bind mode, not Host header
# ---------------------------------------------------------------------------


class TestBindModeEnvGate:
    """_apply_bind_mode_env publishes JOBSMITH_PUBLIC_BIND for non-loopback binds."""

    def test_loopback_host_clears_flag(self):
        from jobsmith.api.server import _apply_bind_mode_env
        from jobsmith.api.staticui import PUBLIC_BIND_ENV_VAR

        with patch.dict(os.environ):
            os.environ[PUBLIC_BIND_ENV_VAR] = "1"  # stale value must be cleared
            _apply_bind_mode_env("127.0.0.1")
            assert PUBLIC_BIND_ENV_VAR not in os.environ

    def test_public_host_sets_flag(self):
        from jobsmith.api.server import _apply_bind_mode_env
        from jobsmith.api.staticui import PUBLIC_BIND_ENV_VAR

        with patch.dict(os.environ):
            os.environ.pop(PUBLIC_BIND_ENV_VAR, None)
            _apply_bind_mode_env("0.0.0.0")
            assert os.environ.get(PUBLIC_BIND_ENV_VAR) == "1"

    def test_specific_lan_ip_is_public(self):
        """A bind to a concrete LAN IP (not 0.0.0.0) is still treated as public."""
        from jobsmith.api.server import _apply_bind_mode_env
        from jobsmith.api.staticui import PUBLIC_BIND_ENV_VAR

        with patch.dict(os.environ):
            os.environ.pop(PUBLIC_BIND_ENV_VAR, None)
            _apply_bind_mode_env("192.168.1.10")
            assert os.environ.get(PUBLIC_BIND_ENV_VAR) == "1"

    def test_up_bind_public_sets_flag_before_uvicorn(self):
        """`up --bind-public` sets JOBSMITH_PUBLIC_BIND=1 before uvicorn.run."""
        from jobsmith.api.staticui import PUBLIC_BIND_ENV_VAR

        captured: dict[str, str | None] = {}

        def capture_env_run(*a, **kw):
            captured["flag"] = os.environ.get(PUBLIC_BIND_ENV_VAR)

        with (
            patch.dict(os.environ),
            patch("jobsmith.api.server.uvicorn") as mock_uv_mod,
            patch("jobsmith.api.server._wait_for_port", MagicMock()),
        ):
            os.environ.pop(PUBLIC_BIND_ENV_VAR, None)
            mock_uv_mod.run = capture_env_run
            result = runner.invoke(app, ["up", "--bind-public", "--no-open"])
            assert result.exit_code == 0, result.output

        assert captured.get("flag") == "1", (
            f"Expected JOBSMITH_PUBLIC_BIND=1 at uvicorn.run time, got: {captured.get('flag')!r}"
        )


# ---------------------------------------------------------------------------
# TestUpWebbrowserFailureContinues
# ---------------------------------------------------------------------------


class TestUpWebbrowserFailureContinues:
    """webbrowser.open raises → URL logged, server continues (no crash)."""

    def test_webbrowser_failure_does_not_crash(self):
        """If webbrowser.open raises, the command exits 0 (server ran normally)."""

        def fake_wait(host, port, *, timeout):
            pass  # instant

        def bad_open(url):
            raise OSError("no browser on this system")

        with (
            patch("jobsmith.api.server.uvicorn") as mock_uv_mod,
            patch("jobsmith.api.server.webbrowser") as mock_wb_mod,
            patch("jobsmith.api.server._wait_for_port", fake_wait),
        ):
            mock_uv_mod.run = MagicMock(return_value=None)
            mock_wb_mod.open = bad_open

            result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, (
            f"Expected exit 0 even when webbrowser.open fails. Got: {result.exit_code}. "
            f"Output: {result.output}"
        )

    def test_webbrowser_failure_logs_url(self, caplog):
        """When webbrowser.open fails, the fallback URL is logged (not raised).

        Unit-tests the browser-opener helper directly so there is no race with
        the daemon thread that runs it in the real `up` flow.
        """
        from jobsmith.api.server import _open_browser_after_listen

        with (
            patch("jobsmith.api.server.webbrowser") as mock_wb_mod,
            patch("jobsmith.api.server._wait_for_port", MagicMock()),
            caplog.at_level(logging.WARNING, logger="jobsmith.api.server"),
        ):
            mock_wb_mod.open = MagicMock(side_effect=OSError("no browser"))
            # Must not raise even though webbrowser.open fails.
            _open_browser_after_listen("http://127.0.0.1:8000", "127.0.0.1", 8000)

        assert any(
            "8000" in r.getMessage() or "http" in r.getMessage()
            for r in caplog.records
        ), f"Expected fallback URL in logs. Records: {[r.getMessage() for r in caplog.records]}"
