"""Tests for the asyncio RunSupervisor (feat-cf348e05).

The supervisor spawns and tracks ``jobsmith apply`` (or any) subprocesses,
captures stdout/stderr line-by-line, and exposes them to a streaming
consumer (the SSE events endpoint). These tests use simple shell stubs
(``sh -c '...'``) — we are testing the *supervisor mechanics*, not the
``jobsmith apply`` command.

These tests use ``asyncio.run`` directly (no pytest-asyncio dependency in
the project). Each test wraps its body in an ``async def _run()`` and
invokes ``asyncio.run(_run())`` from a sync test function.

The supervisor-to-SSE bridge is *not* covered here as a synchronous
integration test (TestClient + cross-loop subprocess transport is fragile);
the upcoming POST /run slice will own that integration test once it can
spawn the supervisor on the same loop the SSE generator is consuming from.

Coverage (six tests, per the slice description)
-----------------------------------------------
1. test_start_returns_run_id_and_tracks_process
2. test_stdout_captured_line_by_line
3. test_stderr_captured
4. test_exit_code_recorded
5. test_kill_terminates_running_process
6. test_registry_caps_buffer
"""
from __future__ import annotations

import asyncio
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _wait_for_status(
    supervisor, run_id: str, target: str, timeout_s: float = 5.0
) -> None:
    """Poll the supervisor until ``run_id``'s handle reaches ``target``."""
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout_s
    while loop.time() < end:
        handle = supervisor.get(run_id)
        if handle is not None and handle.status == target:
            return
        await asyncio.sleep(0.02)
    handle = supervisor.get(run_id)
    raise AssertionError(
        f"timeout waiting for status={target!r}, got "
        f"{handle.status if handle else None!r}"
    )


async def _drain_stream(supervisor, run_id: str, timeout_s: float = 5.0) -> list:
    """Collect every LogLine yielded for ``run_id`` until the stream ends."""
    out = []

    async def _consume() -> None:
        async for line in supervisor.stream(run_id):
            out.append(line)

    await asyncio.wait_for(_consume(), timeout=timeout_s)
    return out


# ---------------------------------------------------------------------------
# 1. test_start_returns_run_id_and_tracks_process
# ---------------------------------------------------------------------------


def test_start_returns_run_id_and_tracks_process(tmp_path: Path) -> None:
    from jobsmith.api.supervisor import RunSupervisor

    async def _run() -> None:
        sup = RunSupervisor()
        run_id = await sup.start(
            slug="acme",
            argv=["sh", "-c", "sleep 0.05"],
            cwd=tmp_path,
        )

        assert isinstance(run_id, str) and run_id
        handle = sup.get(run_id)
        assert handle is not None
        assert handle.run_id == run_id
        assert handle.slug == "acme"
        assert handle.status in ("running", "done")
        assert handle.started_at  # ISO 8601 string

        await _wait_for_status(sup, run_id, "done")
        handle = sup.get(run_id)
        assert handle.status == "done"
        assert handle.exit_code == 0
        assert handle.finished_at is not None

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 2. test_stdout_captured_line_by_line
# ---------------------------------------------------------------------------


def test_stdout_captured_line_by_line(tmp_path: Path) -> None:
    from jobsmith.api.supervisor import RunSupervisor

    async def _run() -> None:
        sup = RunSupervisor()
        run_id = await sup.start(
            slug="acme",
            argv=["sh", "-c", "echo hello; echo world"],
            cwd=tmp_path,
        )

        lines = await _drain_stream(sup, run_id, timeout_s=5.0)
        stdout_lines = [ll for ll in lines if ll.stream == "stdout"]
        assert [ll.line for ll in stdout_lines] == ["hello", "world"]
        for ll in stdout_lines:
            assert ll.timestamp  # ISO 8601 set

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 3. test_stderr_captured
# ---------------------------------------------------------------------------


def test_stderr_captured(tmp_path: Path) -> None:
    from jobsmith.api.supervisor import RunSupervisor

    async def _run() -> None:
        sup = RunSupervisor()
        run_id = await sup.start(
            slug="acme",
            argv=["sh", "-c", "echo err >&2"],
            cwd=tmp_path,
        )

        lines = await _drain_stream(sup, run_id, timeout_s=5.0)
        stderr_lines = [ll for ll in lines if ll.stream == "stderr"]
        assert any(ll.line == "err" for ll in stderr_lines)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 4. test_exit_code_recorded
# ---------------------------------------------------------------------------


def test_exit_code_recorded(tmp_path: Path) -> None:
    from jobsmith.api.supervisor import RunSupervisor

    async def _run() -> None:
        sup = RunSupervisor()
        run_id = await sup.start(
            slug="acme",
            argv=["sh", "-c", "exit 1"],
            cwd=tmp_path,
        )

        await _wait_for_status(sup, run_id, "failed")
        handle = sup.get(run_id)
        assert handle.exit_code == 1
        assert handle.status == "failed"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 5. test_kill_terminates_running_process
# ---------------------------------------------------------------------------


def test_kill_terminates_running_process(tmp_path: Path) -> None:
    from jobsmith.api.supervisor import RunSupervisor

    async def _run() -> None:
        sup = RunSupervisor()
        run_id = await sup.start(
            slug="acme",
            argv=["sh", "-c", "sleep 30"],
            cwd=tmp_path,
        )

        # Give the process a beat to start.
        await asyncio.sleep(0.1)
        handle = sup.get(run_id)
        assert handle.status == "running"

        killed = await sup.kill(run_id, timeout_s=2.0)
        assert killed is True

        handle = sup.get(run_id)
        assert handle.status == "killed"
        assert handle.finished_at is not None

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 6. test_registry_caps_buffer
# ---------------------------------------------------------------------------


def test_registry_caps_buffer(tmp_path: Path) -> None:
    """A producer that emits more than ``max_buffered_lines`` should retain
    only the most recent N lines in the buffer."""
    from jobsmith.api.supervisor import RunSupervisor

    cap = 100
    extra = 100
    total = cap + extra

    async def _run() -> None:
        sup = RunSupervisor(max_buffered_lines=cap)
        run_id = await sup.start(
            slug="acme",
            argv=[
                "sh",
                "-c",
                f"i=1; while [ $i -le {total} ]; do echo $i; i=$((i+1)); done",
            ],
            cwd=tmp_path,
        )

        # Wait for the process to finish entirely so all lines are in buffer.
        await _wait_for_status(sup, run_id, "done", timeout_s=5.0)
        # Allow the drain tasks to finalise.
        await asyncio.sleep(0.1)

        # Stream — should yield only the last `cap` lines.
        lines = await _drain_stream(sup, run_id, timeout_s=5.0)
        stdout_lines = [ll for ll in lines if ll.stream == "stdout"]
        assert len(stdout_lines) == cap
        # The most recent line should be the highest-numbered.
        assert stdout_lines[-1].line == str(total)
        # The first should be (total - cap + 1).
        assert stdout_lines[0].line == str(total - cap + 1)

    asyncio.run(_run())
