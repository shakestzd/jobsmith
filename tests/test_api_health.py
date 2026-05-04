"""Tests for the FastAPI health endpoint and CORS configuration.

TDD: these tests are written before the implementation exists.
Run: uv run pytest tests/test_api_health.py
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jobsmith.api.main import create_app


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    return TestClient(app)


def test_health_returns_200_with_required_fields(client: TestClient) -> None:
    """GET /health returns 200 and a JSON body with all required keys."""
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert "version" in body
    assert "git_sha" in body
    assert "db_ok" in body
    assert "master_ok" in body


def test_cors_allows_localhost_5173(client: TestClient) -> None:
    """OPTIONS preflight from http://localhost:5173 receives CORS allow header."""
    response = client.options(
        "/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"
