"""Tests for /api/config endpoints.

Coverage:
1. GET /api/config without auth → 401
2. GET /api/config returns config dict (mocked load_config)
3. POST /api/config/validate with invalid body → 200 ok=false errors=[...]
4. POST /api/config/validate with valid body → 200 ok=true errors=[]
5. PUT /api/config with valid body → 200 and `.apply-config.yaml` is written
6. PUT /api/config with invalid body → 422 (no file written)
7. GET /api/config wrong token → 401
8. PUT /api/config returns saved config on success
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient

from jobsmith.api.auth import TOKEN_ENV_VAR, _get_expected_token
from jobsmith.api.main import create_app
from jobsmith.config import JobsmithConfig

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_token_cache():
    """Reset cached token between tests."""
    _get_expected_token.cache_clear()
    yield
    _get_expected_token.cache_clear()


TOKEN = "test-config-token-abc"


@pytest.fixture()
def client():
    """TestClient with a known Bearer token set via env."""
    with patch.dict(os.environ, {TOKEN_ENV_VAR: TOKEN}):
        app = create_app()
        yield TestClient(app, raise_server_exceptions=True)


def _auth(tok: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


_VALID_CONFIG_DATA: dict = {
    "user": {
        "name": "Pat Doe",
        "email": "pat@example.com",
    }
}

_INVALID_CONFIG_DATA: dict = {
    "anchor_thresholds": {
        "percent_min": 999.0,  # out of range — must be 0-100
    }
}


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


def test_get_config_no_auth_returns_401(client: TestClient) -> None:
    """Missing token → 401."""
    resp = client.get("/api/config")
    assert resp.status_code == 401


def test_get_config_wrong_token_returns_401(client: TestClient) -> None:
    """Wrong token → 401."""
    resp = client.get("/api/config", headers=_auth("bad-token"))
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/config
# ---------------------------------------------------------------------------


def test_get_config_returns_200_with_dict(client: TestClient) -> None:
    """Authenticated GET → 200 with a JSON object."""
    mock_config = JobsmithConfig()
    with patch("jobsmith.api.config.load_config", return_value=mock_config):
        resp = client.get("/api/config", headers=_auth())
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)


def test_get_config_returns_expected_fields(client: TestClient) -> None:
    """Response includes known top-level keys from JobsmithConfig."""
    mock_config = JobsmithConfig()
    with patch("jobsmith.api.config.load_config", return_value=mock_config):
        resp = client.get("/api/config", headers=_auth())
    data = resp.json()
    assert "user" in data
    assert "master" in data
    assert "voice" in data
    assert "cover_letter" in data


def test_get_config_returns_user_name(client: TestClient) -> None:
    """User identity fields are forwarded from load_config output."""
    mock_config = JobsmithConfig.model_validate({"user": {"name": "Ada Lovelace"}})
    with patch("jobsmith.api.config.load_config", return_value=mock_config):
        resp = client.get("/api/config", headers=_auth())
    assert resp.json()["user"]["name"] == "Ada Lovelace"


# ---------------------------------------------------------------------------
# POST /api/config/validate
# ---------------------------------------------------------------------------


def test_post_validate_no_auth_returns_401(client: TestClient) -> None:
    """Missing token → 401."""
    resp = client.post(
        "/api/config/validate",
        content=yaml.safe_dump(_VALID_CONFIG_DATA),
        headers={"Content-Type": "application/x-yaml"},
    )
    assert resp.status_code == 401


def test_post_validate_valid_config_returns_ok_true(client: TestClient) -> None:
    """Valid config body → 200 ok=true errors=[]."""
    resp = client.post(
        "/api/config/validate",
        content=yaml.safe_dump(_VALID_CONFIG_DATA),
        headers={**_auth(), "Content-Type": "application/x-yaml"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["errors"] == []


def test_post_validate_invalid_config_returns_ok_false(client: TestClient) -> None:
    """Invalid config body → 200 ok=false with errors list."""
    resp = client.post(
        "/api/config/validate",
        content=yaml.safe_dump(_INVALID_CONFIG_DATA),
        headers={**_auth(), "Content-Type": "application/x-yaml"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert len(body["errors"]) > 0


def test_post_validate_errors_have_field_and_message(client: TestClient) -> None:
    """Each error object has 'field' and 'message' keys."""
    resp = client.post(
        "/api/config/validate",
        content=yaml.safe_dump(_INVALID_CONFIG_DATA),
        headers={**_auth(), "Content-Type": "application/x-yaml"},
    )
    error = resp.json()["errors"][0]
    assert "field" in error
    assert "message" in error


def test_post_validate_json_body_accepted(client: TestClient) -> None:
    """JSON body (superset of YAML) is also accepted."""
    resp = client.post(
        "/api/config/validate",
        content=json.dumps(_VALID_CONFIG_DATA),
        headers={**_auth(), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# PUT /api/config
# ---------------------------------------------------------------------------


def test_put_config_no_auth_returns_401(client: TestClient) -> None:
    """Missing token → 401."""
    resp = client.put(
        "/api/config",
        content=yaml.safe_dump(_VALID_CONFIG_DATA),
        headers={"Content-Type": "application/x-yaml"},
    )
    assert resp.status_code == 401


def test_put_config_valid_writes_file(client: TestClient, tmp_path: Path) -> None:
    """PUT with valid config writes `.apply-config.yaml` to cwd."""
    config_file = tmp_path / ".apply-config.yaml"
    assert not config_file.exists()

    with patch("jobsmith.api.config.Path") as mock_path_cls:
        # Make Path.cwd() return tmp_path so we write to tmp_path
        mock_path_cls.cwd.return_value = tmp_path
        mock_path_cls.side_effect = lambda *args: Path(*args)  # passthrough for other uses
        resp = client.put(
            "/api/config",
            content=yaml.safe_dump(_VALID_CONFIG_DATA),
            headers={**_auth(), "Content-Type": "application/x-yaml"},
        )

    assert resp.status_code == 200
    assert config_file.exists()


def test_put_config_valid_returns_config_dict(client: TestClient, tmp_path: Path) -> None:
    """PUT with valid config returns the saved config as JSON."""
    with patch("jobsmith.api.config.Path") as mock_path_cls:
        mock_path_cls.cwd.return_value = tmp_path
        mock_path_cls.side_effect = lambda *args: Path(*args)
        resp = client.put(
            "/api/config",
            content=yaml.safe_dump(_VALID_CONFIG_DATA),
            headers={**_auth(), "Content-Type": "application/x-yaml"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)
    assert data["user"]["name"] == "Pat Doe"


def test_put_config_invalid_returns_422(client: TestClient) -> None:
    """PUT with invalid config → 422, no file written."""
    resp = client.put(
        "/api/config",
        content=yaml.safe_dump(_INVALID_CONFIG_DATA),
        headers={**_auth(), "Content-Type": "application/x-yaml"},
    )
    assert resp.status_code == 422


def test_put_config_invalid_does_not_write_file(client: TestClient, tmp_path: Path) -> None:
    """PUT with invalid config must NOT write any file."""
    config_file = tmp_path / ".apply-config.yaml"

    with patch("jobsmith.api.config.Path") as mock_path_cls:
        mock_path_cls.cwd.return_value = tmp_path
        mock_path_cls.side_effect = lambda *args: Path(*args)
        client.put(
            "/api/config",
            content=yaml.safe_dump(_INVALID_CONFIG_DATA),
            headers={**_auth(), "Content-Type": "application/x-yaml"},
        )

    assert not config_file.exists()


# ---------------------------------------------------------------------------
# LLM settings round-trip
# ---------------------------------------------------------------------------

_LLM_OPENAI_COMPAT_DATA: dict = {
    "llm": {
        "provider": "openai_compatible",
        "base_url": "http://127.0.0.1:8080/v1",
        "model": "mlx-community/Llama-3.2-3B-Instruct-4bit",
    }
}


def test_get_config_returns_llm_field(client: TestClient) -> None:
    """GET /api/config response includes an 'llm' key."""
    mock_config = JobsmithConfig()
    with patch("jobsmith.api.config.load_config", return_value=mock_config):
        resp = client.get("/api/config", headers=_auth())
    assert resp.status_code == 200
    data = resp.json()
    assert "llm" in data


def test_get_config_default_llm_provider_is_claude_cli(client: TestClient) -> None:
    """Default LLM provider is 'claude_cli' (backward-compatible)."""
    mock_config = JobsmithConfig()
    with patch("jobsmith.api.config.load_config", return_value=mock_config):
        resp = client.get("/api/config", headers=_auth())
    assert resp.json()["llm"]["provider"] == "claude_cli"


def test_put_config_llm_openai_compat_round_trip(client: TestClient, tmp_path: Path) -> None:
    """PUT with openai_compatible llm body → response includes correct llm settings."""
    with patch("jobsmith.api.config.Path") as mock_path_cls:
        mock_path_cls.cwd.return_value = tmp_path
        mock_path_cls.side_effect = lambda *args: Path(*args)
        resp = client.put(
            "/api/config",
            content=yaml.safe_dump(_LLM_OPENAI_COMPAT_DATA),
            headers={**_auth(), "Content-Type": "application/x-yaml"},
        )
    assert resp.status_code == 200
    llm = resp.json()["llm"]
    assert llm["provider"] == "openai_compatible"
    assert llm["base_url"] == "http://127.0.0.1:8080/v1"
    assert llm["model"] == "mlx-community/Llama-3.2-3B-Instruct-4bit"


def test_put_config_openai_compat_missing_base_url_returns_422(client: TestClient) -> None:
    """PUT openai_compatible without base_url → 422 (LLMSettings validator)."""
    resp = client.put(
        "/api/config",
        content=yaml.safe_dump({"llm": {"provider": "openai_compatible"}}),
        headers={**_auth(), "Content-Type": "application/x-yaml"},
    )
    assert resp.status_code == 422


def test_put_config_preserves_default_provider_on_empty_llm(client: TestClient, tmp_path: Path) -> None:
    """PUT without an llm block → response still has llm.provider == 'claude_cli'."""
    with patch("jobsmith.api.config.Path") as mock_path_cls:
        mock_path_cls.cwd.return_value = tmp_path
        mock_path_cls.side_effect = lambda *args: Path(*args)
        resp = client.put(
            "/api/config",
            content=yaml.safe_dump(_VALID_CONFIG_DATA),
            headers={**_auth(), "Content-Type": "application/x-yaml"},
        )
    assert resp.status_code == 200
    assert resp.json()["llm"]["provider"] == "claude_cli"


# ---------------------------------------------------------------------------
# llm.apply round-trip (ApplySettings)
# ---------------------------------------------------------------------------

_APPLY_CLAUDE_CLOUD_DATA: dict = {
    "llm": {
        "provider": "claude_cli",
        "apply": {
            "orchestrator": "claude_cloud",
            "on_failure": "error",
        },
    }
}

_APPLY_CODE_LOCAL_DATA: dict = {
    "llm": {
        "provider": "claude_cli",
        "apply": {
            "orchestrator": "code_local",
            "on_failure": "fallback_cloud",
            "node_backend": {
                "provider": "openai_compatible",
                "base_url": "http://127.0.0.1:8081/v1",
                "model": "mlx-community/gemma-4-E4B-it-qat-4bit",
            },
        },
    }
}


def test_get_config_default_apply_orchestrator_is_claude_cloud(client: TestClient) -> None:
    """Default apply orchestrator is 'claude_cloud' (backward-compatible)."""
    mock_config = JobsmithConfig()
    with patch("jobsmith.api.config.load_config", return_value=mock_config):
        resp = client.get("/api/config", headers=_auth())
    assert resp.status_code == 200
    apply = resp.json()["llm"]["apply"]
    assert apply["orchestrator"] == "claude_cloud"
    assert apply["on_failure"] == "error"


def test_put_config_apply_claude_cloud_round_trip(client: TestClient, tmp_path: Path) -> None:
    """PUT llm.apply with claude_cloud orchestrator → response round-trips the block."""
    with patch("jobsmith.api.config.Path") as mock_path_cls:
        mock_path_cls.cwd.return_value = tmp_path
        mock_path_cls.side_effect = lambda *args: Path(*args)
        resp = client.put(
            "/api/config",
            content=yaml.safe_dump(_APPLY_CLAUDE_CLOUD_DATA),
            headers={**_auth(), "Content-Type": "application/x-yaml"},
        )
    assert resp.status_code == 200
    apply = resp.json()["llm"]["apply"]
    assert apply["orchestrator"] == "claude_cloud"
    assert apply["on_failure"] == "error"


def test_put_config_apply_code_local_round_trip(client: TestClient, tmp_path: Path) -> None:
    """PUT llm.apply with code_local + node_backend → all fields round-trip correctly."""
    with patch("jobsmith.api.config.Path") as mock_path_cls:
        mock_path_cls.cwd.return_value = tmp_path
        mock_path_cls.side_effect = lambda *args: Path(*args)
        resp = client.put(
            "/api/config",
            content=yaml.safe_dump(_APPLY_CODE_LOCAL_DATA),
            headers={**_auth(), "Content-Type": "application/x-yaml"},
        )
    assert resp.status_code == 200
    apply = resp.json()["llm"]["apply"]
    assert apply["orchestrator"] == "code_local"
    assert apply["on_failure"] == "fallback_cloud"
    nb = apply["node_backend"]
    assert nb["provider"] == "openai_compatible"
    assert nb["base_url"] == "http://127.0.0.1:8081/v1"
    assert nb["model"] == "mlx-community/gemma-4-E4B-it-qat-4bit"


def test_put_config_apply_code_local_missing_base_url_returns_422(client: TestClient) -> None:
    """code_local node_backend with openai_compatible but no base_url → 422."""
    bad_data: dict = {
        "llm": {
            "provider": "claude_cli",
            "apply": {
                "orchestrator": "code_local",
                "node_backend": {
                    "provider": "openai_compatible",
                    # base_url intentionally absent
                },
            },
        }
    }
    resp = client.put(
        "/api/config",
        content=yaml.safe_dump(bad_data),
        headers={**_auth(), "Content-Type": "application/x-yaml"},
    )
    assert resp.status_code == 422


def test_put_config_apply_invalid_orchestrator_returns_422(client: TestClient) -> None:
    """Unknown orchestrator value → 422 from Pydantic literal validation."""
    bad_data: dict = {
        "llm": {
            "apply": {
                "orchestrator": "not_a_valid_orchestrator",
            }
        }
    }
    resp = client.put(
        "/api/config",
        content=yaml.safe_dump(bad_data),
        headers={**_auth(), "Content-Type": "application/x-yaml"},
    )
    assert resp.status_code == 422
