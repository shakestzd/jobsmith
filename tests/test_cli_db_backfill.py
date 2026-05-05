"""Tests for `jobsmith db backfill` CLI subcommand (feat-7a787f6c).

All DB-touching functions are mocked so no real database is needed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MOCK_CONN = MagicMock(name="conn")

_DB_OPEN_PATH = "jobsmith.cli.open_pipeline_db"
_BACKFILL_SLUG_PATH = "jobsmith.cli.backfill_slug"
_BACKFILL_ALL_PATH = "jobsmith.cli.backfill_all"
_ITER_SLUGS_PATH = "jobsmith.cli.iter_backfillable_slugs"


def _invoke_db_backfill(runner: CliRunner, extra_args: list[str], *, tmp_path):
    """Invoke `jobsmith db backfill` with a fake repo (config + DB present)."""
    from jobsmith.cli import app

    # Provide a minimal config so load_config() + repo_root_for() succeed.
    config_file = tmp_path / ".apply-config.yaml"
    config_file.write_text(
        "output:\n"
        "  applications_dir: private/applications\n"
        "  job_search_db: private/job_search.db\n"
        "  jobsmith_db: private/jobsmith.db\n"
    )
    # DB must exist so the CLI can open it.
    db_path = tmp_path / "private" / "jobsmith.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.touch()

    with patch("jobsmith.cli.find_config", return_value=config_file), \
         patch("jobsmith.cli.repo_root_for", return_value=tmp_path), \
         patch(_DB_OPEN_PATH, return_value=_MOCK_CONN):
        result = runner.invoke(app, ["db", "backfill"] + extra_args)
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDbBackfillSlug:
    def test_slug_calls_backfill_slug_once(self, runner: CliRunner, tmp_path) -> None:
        with patch(_BACKFILL_SLUG_PATH, return_value=3) as mock_bs:
            result = _invoke_db_backfill(runner, ["--slug", "acme-swe"], tmp_path=tmp_path)

        assert result.exit_code == 0, result.output
        mock_bs.assert_called_once()
        args, _kwargs = mock_bs.call_args
        # backfill_slug(conn, slug, applications_dir)
        assert args[1] == "acme-swe"

    def test_slug_output_contains_slug(self, runner: CliRunner, tmp_path) -> None:
        with patch(_BACKFILL_SLUG_PATH, return_value=2):
            result = _invoke_db_backfill(runner, ["--slug", "some-slug"], tmp_path=tmp_path)

        assert "some-slug" in result.output


class TestDbBackfillAll:
    def test_all_calls_backfill_all_once(self, runner: CliRunner, tmp_path) -> None:
        with patch(_BACKFILL_ALL_PATH, return_value={"acme-swe": 5}) as mock_ba:
            result = _invoke_db_backfill(runner, ["--all"], tmp_path=tmp_path)

        assert result.exit_code == 0, result.output
        mock_ba.assert_called_once()

    def test_all_output_shows_summary(self, runner: CliRunner, tmp_path) -> None:
        with patch(_BACKFILL_ALL_PATH, return_value={"acme-swe": 5, "beta-co": 0}):
            result = _invoke_db_backfill(runner, ["--all"], tmp_path=tmp_path)

        assert result.exit_code == 0, result.output
        assert "2" in result.output or "backfill" in result.output.lower()


class TestDbBackfillNoArgs:
    def test_no_args_iterates_and_backfills_each(self, runner: CliRunner, tmp_path) -> None:
        slugs = ["acme-swe", "beta-co", "gamma-inc"]
        with patch(_ITER_SLUGS_PATH, return_value=slugs) as mock_iter, \
             patch(_BACKFILL_SLUG_PATH, return_value=1) as mock_bs:
            result = _invoke_db_backfill(runner, [], tmp_path=tmp_path)

        assert result.exit_code == 0, result.output
        mock_iter.assert_called_once()
        assert mock_bs.call_count == len(slugs)
        called_slugs = [c.args[1] for c in mock_bs.call_args_list]
        assert called_slugs == slugs

    def test_no_args_empty_slugs_exits_ok(self, runner: CliRunner, tmp_path) -> None:
        with patch(_ITER_SLUGS_PATH, return_value=[]), \
             patch(_BACKFILL_SLUG_PATH) as mock_bs:
            result = _invoke_db_backfill(runner, [], tmp_path=tmp_path)

        assert result.exit_code == 0, result.output
        mock_bs.assert_not_called()


class TestDbBackfillMutualExclusion:
    def test_slug_and_all_together_exits_nonzero(self, runner: CliRunner, tmp_path) -> None:
        with patch(_BACKFILL_SLUG_PATH), patch(_BACKFILL_ALL_PATH):
            result = _invoke_db_backfill(
                runner, ["--slug", "acme-swe", "--all"], tmp_path=tmp_path
            )

        assert result.exit_code != 0
