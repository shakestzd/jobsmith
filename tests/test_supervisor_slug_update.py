"""Tests for supervisor slug update and alias resolution (bug-84eaa5a1).

Verifies that:
1. update_slug() remaps active-by-slug and creates aliases
2. Stale slugs resolve to canonical slugs via aliases
3. Aliases are cleaned up after run completion
4. Duplicate-launch conflicts work across rekeyed slugs
5. SSE stream() continues working for stale slugs
"""
from __future__ import annotations

import asyncio

import pytest

from jobsmith.api.supervisor import RunSupervisor
from jobsmith.core.events import PipelineEvent


class TestSupervisorSlugUpdate:
    """Verify supervisor.update_slug() handles directory renames correctly."""

    def test_update_slug_remaps_active_by_slug(self) -> None:
        """update_slug moves the active mapping from old to new slug."""
        sup = RunSupervisor()
        sup.register_run(run_id="r1", slug="starting-slug")

        # Before update, active lookup uses starting slug
        assert sup.get_active_for_slug("starting-slug") == "r1"

        # After update
        sup.update_slug("starting-slug", "canonical-slug", "r1")

        # New slug is active
        assert sup.get_active_for_slug("canonical-slug") == "r1"
        # Old slug is still resolvable via alias
        assert sup.get_active_for_slug("starting-slug") == "r1"

    def test_update_slug_creates_alias(self) -> None:
        """update_slug creates a stale→canonical mapping."""
        sup = RunSupervisor()
        sup.register_run(run_id="r2", slug="stale")
        sup.update_slug("stale", "canonical", "r2")

        # Check that the alias was registered
        assert "stale" in sup._slug_aliases
        assert sup._slug_aliases["stale"] == "canonical"

    def test_update_slug_updates_handle(self) -> None:
        """update_slug updates the RunHandle's slug field."""
        sup = RunSupervisor()
        sup.register_run(run_id="r3", slug="old")
        handle = sup.get("r3")
        assert handle.slug == "old"

        sup.update_slug("old", "new", "r3")

        handle = sup.get("r3")
        assert handle.slug == "new"

    def test_update_slug_noop_when_same(self) -> None:
        """update_slug is a no-op when old==new."""
        sup = RunSupervisor()
        sup.register_run(run_id="r4", slug="slug")

        # Call with same slug
        sup.update_slug("slug", "slug", "r4")

        # Should have no aliases
        assert len(sup._slug_aliases) == 0
        assert sup.get_active_for_slug("slug") == "r4"

    def test_update_slug_unknown_run_is_safe(self) -> None:
        """update_slug with unknown run_id is a no-op (no exception)."""
        sup = RunSupervisor()
        # Call with non-existent run_id
        sup.update_slug("old", "new", "does-not-exist")
        # Must not raise and should not create aliases for unknown runs
        assert len(sup._slug_aliases) == 0

    def test_duplicate_launch_blocked_on_canonical_slug(self) -> None:
        """Trying to launch with canonical slug conflicts with active run."""
        sup = RunSupervisor()
        sup.register_run(run_id="r5", slug="start")
        sup.update_slug("start", "canonical", "r5")

        # Now trying to register a new run with the canonical slug should fail
        active = sup.get_active_for_slug("canonical")
        assert active == "r5", "Should find active run via canonical slug"

    def test_duplicate_launch_blocked_on_stale_slug(self) -> None:
        """Trying to launch with stale slug conflicts with active run."""
        sup = RunSupervisor()
        sup.register_run(run_id="r6", slug="start")
        sup.update_slug("start", "canonical", "r6")

        # Now trying to register a new run with the stale slug should fail
        active = sup.get_active_for_slug("start")
        assert active == "r6", "Should find active run via stale slug alias"

    def test_on_run_complete_cleans_up_aliases(self) -> None:
        """on_run_complete removes aliases when the run terminates."""
        sup = RunSupervisor()
        sup.register_run(run_id="r7", slug="start")
        sup.update_slug("start", "canonical", "r7")

        assert "start" in sup._slug_aliases

        sup.on_run_complete("r7", rc=0)

        # Alias should be removed
        assert "start" not in sup._slug_aliases
        # Can't find the run anymore
        assert sup.get_active_for_slug("start") is None

    @pytest.mark.anyio
    async def test_kill_cleans_up_aliases(self) -> None:
        """kill() removes aliases when the run is cancelled."""
        sup = RunSupervisor()
        sup.register_run(run_id="r8", slug="start")
        sup.update_slug("start", "canonical", "r8")

        # Create and register a task
        async def dummy_task():
            await asyncio.sleep(0.1)

        task = asyncio.create_task(dummy_task())
        sup.set_task("r8", task)

        assert "start" in sup._slug_aliases

        # Kill the run
        await sup.kill("r8")

        # Alias should be removed
        assert "start" not in sup._slug_aliases

    @pytest.mark.anyio
    async def test_stream_works_after_rekey(self) -> None:
        """stream() continues to work after slug is updated."""
        sup = RunSupervisor()
        sink = sup.register_run(run_id="r9", slug="start")

        # Emit an event before rekey
        sink.emit(PipelineEvent(kind="phase_started", phase="gather"))

        # Rekey the slug
        sup.update_slug("start", "canonical", "r9")

        # Collected events should include the pre-rekey event
        collected = []
        async for item in sup.stream("r9"):
            collected.append(item)
            # Don't wait for actual completion
            break

        assert len(collected) > 0
        assert collected[0].payload["type"] == "phase_started"

    @pytest.mark.anyio
    async def test_get_active_for_slug_resolves_stale(self) -> None:
        """get_active_for_slug resolves stale slugs through aliases."""
        sup = RunSupervisor()
        sup.register_run(run_id="r10", slug="stale-slug")

        # Before rekey, both return the run
        assert sup.get_active_for_slug("stale-slug") == "r10"

        sup.update_slug("stale-slug", "canonical-slug", "r10")

        # Both old and new slugs should resolve
        assert sup.get_active_for_slug("stale-slug") == "r10"
        assert sup.get_active_for_slug("canonical-slug") == "r10"

        # After completion, neither should resolve
        sup.on_run_complete("r10", rc=0)
        assert sup.get_active_for_slug("stale-slug") is None
        assert sup.get_active_for_slug("canonical-slug") is None
