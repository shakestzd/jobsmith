"""Tests for `jobsmith artifact put` CLI shim (feat-60be8c3a).

Specialist subprocesses use this shim to PUT artifacts to the DB without
importing the SDK directly.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _build_envelope(*, kind: str, version: int = 1) -> MagicMock:
    env = MagicMock()
    env.kind = kind
    env.version = version
    return env


class TestArtifactPut:
    def test_happy_path_returns_zero(self, runner: CliRunner) -> None:
        from jobsmith.cli import app

        mock_client = MagicMock()
        mock_client.put_artifact.return_value = _build_envelope(kind="jd-parsed")

        with patch("jobsmith.api.client.JobsmithClient", return_value=mock_client):
            result = runner.invoke(
                app,
                [
                    "artifact",
                    "put",
                    "--slug",
                    "acme-swe",
                    "--run",
                    "run-001",
                    "--kind",
                    "jd-parsed",
                    "--json",
                    json.dumps({"company": "Acme"}),
                ],
            )

        assert result.exit_code == 0, result.output
        mock_client.put_artifact.assert_called_once_with(
            "acme-swe", "run-001", "jd-parsed", {"company": "Acme"}
        )
        assert "wrote" in result.output
        assert "jd-parsed" in result.output

    def test_invalid_json_exits_2(self, runner: CliRunner) -> None:
        from jobsmith.cli import app

        result = runner.invoke(
            app,
            [
                "artifact",
                "put",
                "--slug",
                "x",
                "--run",
                "r",
                "--kind",
                "k",
                "--json",
                "{not valid",
            ],
        )
        assert result.exit_code == 2
        assert "invalid --json" in result.output

    def test_non_object_json_exits_2(self, runner: CliRunner) -> None:
        from jobsmith.cli import app

        result = runner.invoke(
            app,
            [
                "artifact",
                "put",
                "--slug",
                "x",
                "--run",
                "r",
                "--kind",
                "k",
                "--json",
                '"just a string"',
            ],
        )
        assert result.exit_code == 2
        assert "object" in result.output

    def test_auth_error_exits_3(self, runner: CliRunner) -> None:
        from jobsmith.api.client import AuthError
        from jobsmith.cli import app

        with patch("jobsmith.api.client.JobsmithClient", side_effect=AuthError("no token")):
            result = runner.invoke(
                app,
                [
                    "artifact",
                    "put",
                    "--slug",
                    "x",
                    "--run",
                    "r",
                    "--kind",
                    "k",
                    "--json",
                    "{}",
                ],
            )
        assert result.exit_code == 3
        assert "auth error" in result.output

    def test_conflict_error_exits_5(self, runner: CliRunner) -> None:
        from jobsmith.api.client import ConflictError
        from jobsmith.cli import app

        mock_client = MagicMock()
        mock_client.put_artifact.side_effect = ConflictError("version mismatch")

        with patch("jobsmith.api.client.JobsmithClient", return_value=mock_client):
            result = runner.invoke(
                app,
                [
                    "artifact",
                    "put",
                    "--slug",
                    "x",
                    "--run",
                    "r",
                    "--kind",
                    "jd-parsed",
                    "--json",
                    "{}",
                ],
            )
        assert result.exit_code == 5
        assert "conflict" in result.output

    def test_not_found_error_exits_4(self, runner: CliRunner) -> None:
        from jobsmith.api.client import NotFoundError
        from jobsmith.cli import app

        mock_client = MagicMock()
        mock_client.put_artifact.side_effect = NotFoundError("no such run")

        with patch("jobsmith.api.client.JobsmithClient", return_value=mock_client):
            result = runner.invoke(
                app,
                [
                    "artifact",
                    "put",
                    "--slug",
                    "x",
                    "--run",
                    "r",
                    "--kind",
                    "jd-parsed",
                    "--json",
                    "{}",
                ],
            )
        assert result.exit_code == 4
        assert "not found" in result.output
