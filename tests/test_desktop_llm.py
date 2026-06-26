"""Tests for desktop offline-mode LLM backend detection (feat-aaa91b6d, slice 7).

Two layers:
  - unit: ``jobsmith.desktop.deps.llm_status`` / ``_probe_openai_models`` with
    ``httpx``/``shutil`` monkeypatched (no live MLX or Ollama server required),
    plus one real fast-fail probe against a closed port.
  - regression (Goal 4, fully automatable): GET /api/desktop/llm/status is 404
    on a normal server and 200 only when JOBSMITH_DESKTOP=1; the deferred
    POST /offline-mode placeholder returns 501 (writes no config).

REDUCED SCOPE (per plan-a23bba5f, slice 7): detection + status only. The actual
pluggable-backend `llm` config + chat/scoring routing is deferred to
plan-938f735b — there is intentionally NO config-writing endpoint here.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jobsmith.api.auth import TOKEN_ENV_VAR, _get_expected_token
from jobsmith.api.main import create_app
from jobsmith.desktop import deps

TOKEN = "test-desktop-llm-token-xyz"


@pytest.fixture(autouse=True)
def _clear_token_cache():
    _get_expected_token.cache_clear()
    yield
    _get_expected_token.cache_clear()


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeResp:
    """Minimal stand-in for an ``httpx.Response`` (status_code + json())."""

    def __init__(self, status_code: int, payload=None, *, raises: bool = False):
        self.status_code = status_code
        self._payload = payload
        self._raises = raises

    def json(self):
        if self._raises:
            raise ValueError("no json body")
        return self._payload


def _refuse(*_a, **_k):
    """Simulate nothing listening on the port (connection refused)."""
    raise deps.httpx.ConnectError("connection refused")


# ---------------------------------------------------------------------------
# Unit: _probe_openai_models
# ---------------------------------------------------------------------------


def test_probe_closed_port_is_unreachable_fast():
    """A real probe at a port nothing can listen on refuses immediately.

    Port 1 is privileged and never bound by a user process, so the connect
    fails fast with no timeout wait — proving the closed-port path is quick.
    """
    reachable, model = deps._probe_openai_models("http://127.0.0.1:1")
    assert reachable is False
    assert model is None


def test_probe_reachable_stub_reports_model(monkeypatch):
    """A 200 OpenAI-compatible /v1/models response ⇒ reachable:true + model id."""
    payload = {"object": "list", "data": [{"id": "qwen2.5-coder", "object": "model"}]}
    monkeypatch.setattr(deps.httpx, "get", lambda *_a, **_k: _FakeResp(200, payload))
    reachable, model = deps._probe_openai_models("http://127.0.0.1:8080")
    assert reachable is True
    assert model == "qwen2.5-coder"


def test_probe_non_200_is_unreachable(monkeypatch):
    """A reachable host that answers non-200 is treated as not usable."""
    monkeypatch.setattr(deps.httpx, "get", lambda *_a, **_k: _FakeResp(503))
    assert deps._probe_openai_models("http://127.0.0.1:8080") == (False, None)


def test_probe_unparseable_json_still_reachable(monkeypatch):
    """A 200 with a non-JSON body is reachable but yields no model id."""
    monkeypatch.setattr(
        deps.httpx, "get", lambda *_a, **_k: _FakeResp(200, raises=True)
    )
    assert deps._probe_openai_models("http://127.0.0.1:8080") == (True, None)


# ---------------------------------------------------------------------------
# Unit: llm_status() — shape, runtime detection, reachability
# ---------------------------------------------------------------------------


def test_llm_status_shape_nothing_running(monkeypatch):
    """No server + no runtimes ⇒ both backends reachable:false, installed:false."""
    monkeypatch.setattr(deps.httpx, "get", _refuse)
    monkeypatch.setattr(deps.shutil, "which", lambda _name: None)
    monkeypatch.setattr(deps, "_module_installed", lambda _name: False)
    snap = deps.llm_status()
    assert set(snap) == {"mlx", "ollama"}
    assert snap["mlx"] == {
        "reachable": False,
        "base_url": "http://127.0.0.1:8080",
        "runtime_installed": False,
        "model": None,
    }
    assert snap["ollama"] == {
        "reachable": False,
        "base_url": "http://127.0.0.1:11434",
        "runtime_installed": False,
        "model": None,
    }


def test_llm_status_ollama_runtime_installed_via_which(monkeypatch):
    """`shutil.which("ollama")` resolving ⇒ ollama.runtime_installed True."""
    monkeypatch.setattr(deps.httpx, "get", _refuse)
    monkeypatch.setattr(deps, "_module_installed", lambda _name: False)
    monkeypatch.setattr(
        deps.shutil,
        "which",
        lambda name: "/usr/local/bin/ollama" if name == "ollama" else None,
    )
    snap = deps.llm_status()
    assert snap["ollama"]["runtime_installed"] is True
    assert snap["mlx"]["runtime_installed"] is False


def test_llm_status_mlx_runtime_installed_via_module(monkeypatch):
    """No binary but an importable `mlx_lm` ⇒ mlx.runtime_installed True."""
    monkeypatch.setattr(deps.httpx, "get", _refuse)
    monkeypatch.setattr(deps.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        deps, "_module_installed", lambda name: name == "mlx_lm"
    )
    snap = deps.llm_status()
    assert snap["mlx"]["runtime_installed"] is True


def test_llm_status_reports_reachable_server(monkeypatch):
    """A stub OpenAI-compatible server makes the matching backend reachable."""
    payload = {"data": [{"id": "llama3.2"}]}

    def _only_ollama(url, *_a, **_k):
        if "11434" in url:
            return _FakeResp(200, payload)
        raise deps.httpx.ConnectError("refused")

    monkeypatch.setattr(deps.httpx, "get", _only_ollama)
    monkeypatch.setattr(deps.shutil, "which", lambda _name: None)
    monkeypatch.setattr(deps, "_module_installed", lambda _name: False)
    snap = deps.llm_status()
    assert snap["ollama"]["reachable"] is True
    assert snap["ollama"]["model"] == "llama3.2"
    assert snap["mlx"]["reachable"] is False


# ---------------------------------------------------------------------------
# Regression (Goal 4): JOBSMITH_DESKTOP gating of /api/desktop/llm/status
# ---------------------------------------------------------------------------


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _client(monkeypatch, *, desktop: bool) -> TestClient:
    monkeypatch.setenv(TOKEN_ENV_VAR, TOKEN)
    if desktop:
        monkeypatch.setenv("JOBSMITH_DESKTOP", "1")
    else:
        monkeypatch.delenv("JOBSMITH_DESKTOP", raising=False)
    _get_expected_token.cache_clear()
    app = create_app()
    return TestClient(app, raise_server_exceptions=True)


def test_llm_status_404_without_desktop_flag(monkeypatch):
    """No JOBSMITH_DESKTOP → the llm router is not mounted → 404."""
    client = _client(monkeypatch, desktop=False)
    resp = client.get("/api/desktop/llm/status", headers=_auth())
    assert resp.status_code == 404


def test_llm_status_200_with_desktop_flag(monkeypatch):
    """JOBSMITH_DESKTOP=1 → router mounted → authenticated 200 with schema.

    httpx is monkeypatched to refuse so the result is deterministic regardless
    of whether the host machine happens to be running Ollama/MLX.
    """
    monkeypatch.setattr(deps.httpx, "get", _refuse)
    monkeypatch.setattr(deps.shutil, "which", lambda _name: None)
    monkeypatch.setattr(deps, "_module_installed", lambda _name: False)
    client = _client(monkeypatch, desktop=True)
    resp = client.get("/api/desktop/llm/status", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["mlx"]["reachable"] is False
    assert body["mlx"]["base_url"] == "http://127.0.0.1:8080"
    assert body["ollama"]["reachable"] is False
    assert body["ollama"]["base_url"] == "http://127.0.0.1:11434"


def test_llm_status_200_reports_reachable_server(monkeypatch):
    """With a stub MLX server the endpoint reports reachable:true + base_url."""
    payload = {"data": [{"id": "mlx-community/Qwen2.5-7B"}]}

    def _only_mlx(url, *_a, **_k):
        if "8080" in url:
            return _FakeResp(200, payload)
        raise deps.httpx.ConnectError("refused")

    monkeypatch.setattr(deps.httpx, "get", _only_mlx)
    monkeypatch.setattr(deps.shutil, "which", lambda _name: None)
    monkeypatch.setattr(deps, "_module_installed", lambda _name: False)
    client = _client(monkeypatch, desktop=True)
    resp = client.get("/api/desktop/llm/status", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["mlx"]["reachable"] is True
    assert body["mlx"]["model"] == "mlx-community/Qwen2.5-7B"


def test_llm_status_requires_auth_when_mounted(monkeypatch):
    """When mounted, /llm/status still enforces the bearer token."""
    client = _client(monkeypatch, desktop=True)
    resp = client.get("/api/desktop/llm/status")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Deferred enable action: POST /offline-mode is a loud 501 placeholder
# ---------------------------------------------------------------------------


def test_offline_mode_501_placeholder_when_mounted(monkeypatch):
    """The enable action returns 501 + a clear pending reason (no config write)."""
    client = _client(monkeypatch, desktop=True)
    resp = client.post("/api/desktop/llm/offline-mode", headers=_auth())
    assert resp.status_code == 501
    body = resp.json()
    assert body["status"] == "unavailable"
    assert "plan-938f735b" in body["reason"]


def test_offline_mode_absent_without_desktop_flag(monkeypatch):
    """No desktop flag → the enable placeholder is not a real route (404/405)."""
    client = _client(monkeypatch, desktop=False)
    resp = client.post("/api/desktop/llm/offline-mode", headers=_auth())
    assert resp.status_code in (404, 405)
