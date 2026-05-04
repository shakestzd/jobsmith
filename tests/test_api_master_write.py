"""Tests for the master-content write endpoints (MVP).

PUT  /api/master/{section}             — replace section with validated body
POST /api/master/{section}/upload      — upload a raw YAML file replacing section

Comment preservation across the parse/dump round-trip is intentionally NOT
tested here — the MVP uses yaml.safe_dump and accepts the loss. The 0.8
DB-as-source-of-truth track owns the ruamel.yaml replacement.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from jobsmith.api.main import create_app

CONFIG_YAML = """\
master:
  work_yml: assets/content/work.yml
  skill_yml: assets/content/skill.yml
  education_yml: assets/content/education.yml
  author_yml: assets/content/author.yml
"""


def _bootstrap(tmp_path: Path) -> None:
    (tmp_path / ".apply-config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    (tmp_path / "assets" / "content").mkdir(parents=True)


# ---------------------------------------------------------------------------
# PUT happy paths
# ---------------------------------------------------------------------------


def test_put_work_replaces_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    body = [
        {
            "title": "Senior Engineer",
            "location": "Acme Corp",
            "date": "2024-Present",
            "description": "Remote",
            "details": ["Shipped many things"],
        }
    ]
    resp = client.put("/api/master/work", json=body)
    assert resp.status_code == 200, resp.text
    written = yaml.safe_load((tmp_path / "assets/content/work.yml").read_text())
    assert isinstance(written, list)
    assert written[0]["title"] == "Senior Engineer"
    assert written[0]["location"] == "Acme Corp"


def test_put_skill_replaces_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    body = [{"title": "Languages", "description": "Python, Go", "details": ["Python", "Go"]}]
    resp = client.put("/api/master/skill", json=body)
    assert resp.status_code == 200
    assert yaml.safe_load((tmp_path / "assets/content/skill.yml").read_text())[0]["title"] == "Languages"


def test_put_education_replaces_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    body = [{"title": "MIT", "location": "Cambridge", "date": "2020-2024", "description": "BSc", "details": []}]
    assert client.put("/api/master/education", json=body).status_code == 200
    written = yaml.safe_load((tmp_path / "assets/content/education.yml").read_text())
    assert written[0]["title"] == "MIT"


def test_put_author_accepts_bare_dict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    body = {"name": {"first": "Pat", "last": "Doe"}, "email": "pat@example.com"}
    resp = client.put("/api/master/author", json=body)
    assert resp.status_code == 200, resp.text
    written = yaml.safe_load((tmp_path / "assets/content/author.yml").read_text())
    # Canonical disk shape always wraps under "author: [...]"
    assert isinstance(written, dict) and isinstance(written["author"], list)
    assert written["author"][0]["email"] == "pat@example.com"


def test_put_author_accepts_canonical_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    body = {"author": [{"name": "Pat Doe", "email": "pat@example.com"}]}
    resp = client.put("/api/master/author", json=body)
    assert resp.status_code == 200
    assert yaml.safe_load((tmp_path / "assets/content/author.yml").read_text()) == body


# ---------------------------------------------------------------------------
# Validation rejection
# ---------------------------------------------------------------------------


def test_put_rejects_wrong_top_level_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    # work expects list
    resp = client.put("/api/master/work", json={"not": "a list"})
    assert resp.status_code == 400


def test_put_rejects_missing_required_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    # WorkEntry requires `title`
    resp = client.put("/api/master/work", json=[{"location": "Acme"}])
    assert resp.status_code == 400


def test_put_invalid_section_returns_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    # FastAPI rejects path params that don't match the Literal at the routing layer
    resp = client.put("/api/master/garbage", json=[])
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Upload happy path + rejections
# ---------------------------------------------------------------------------


def test_upload_replaces_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    yml_text = (
        '- title: "From Upload"\n'
        '  location: "Big Co"\n'
        '  date: "2025"\n'
        '  description: ""\n'
        '  details: []\n'
    )
    resp = client.post(
        "/api/master/work/upload",
        files={"file": ("work.yml", io.BytesIO(yml_text.encode()), "application/x-yaml")},
    )
    assert resp.status_code == 200, resp.text
    written = yaml.safe_load((tmp_path / "assets/content/work.yml").read_text())
    assert written[0]["title"] == "From Upload"


def test_upload_rejects_invalid_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    resp = client.post(
        "/api/master/work/upload",
        files={"file": ("bad.yml", io.BytesIO(b"this: is: invalid: yaml: ::"), "text/plain")},
    )
    assert resp.status_code == 400


def test_upload_rejects_schema_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    # work expects a list of dicts; a top-level scalar is wrong
    resp = client.post(
        "/api/master/work/upload",
        files={"file": ("scalar.yml", io.BytesIO(b"just-a-string\n"), "application/x-yaml")},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Atomic write: the destination is never partially written even on failure
# ---------------------------------------------------------------------------


def test_put_does_not_corrupt_existing_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    # Pre-seed a valid file
    valid = [{"title": "ORIGINAL", "location": "Co", "date": "", "description": "", "details": []}]
    (tmp_path / "assets/content/work.yml").write_text(yaml.safe_dump(valid))
    client = TestClient(create_app())
    # Send invalid payload; pre-existing file must remain intact
    resp = client.put("/api/master/work", json=[{"location": "no-title"}])
    assert resp.status_code == 400
    after = yaml.safe_load((tmp_path / "assets/content/work.yml").read_text())
    assert after[0]["title"] == "ORIGINAL"
