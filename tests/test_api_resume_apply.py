"""Tests for POST /api/applications/{slug}/resume/apply endpoint.

Coverage:
- Valid education YAML parses and validates (using existing lint validator).
- Invalid YAML → 422 invalid_yaml with detail.
- Schema-invalid YAML (wrong type structure) → 422 schema_invalid with detail.
- Disallowed target_file (not one of the four) → 422.
- Page count != 1 → 422 page_count_off with page_count, file rolled back.
- Symlink replaced with real file (copy-on-write).
- Success → applied=true, page_count=1.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobsmith.api.applications import router as applications_router

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_slug(tmp_path: Path):
    """FastAPI app with applications router, using tmp_path for app dirs."""
    app = FastAPI()
    app.include_router(applications_router)

    # Patch _get_app_dir to use tmp_path
    def mock_get_app_dir(slug: str) -> Path | None:
        app_dir = tmp_path / slug
        app_dir.mkdir(parents=True, exist_ok=True)
        return app_dir

    with patch('jobsmith.api.applications._get_app_dir', side_effect=mock_get_app_dir):
        yield app, tmp_path


@pytest.fixture
def client(app_with_slug):
    """TestClient for the app."""
    app, _ = app_with_slug
    return TestClient(app), app_with_slug


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_apply_resume_valid_education_yaml(client):
    """Valid education.yml parses and validates successfully."""
    test_client, (app, tmp_path) = client
    slug = "test-app"

    new_content = """
- school: "University of Chicago"
  degree: "B.S."
  field: "Computer Science"
  graduated: "2020"
"""

    # Mock the render function to return page_count=1
    with patch('jobsmith.api.applications._render_resume', return_value=('ok', 1)):
        response = test_client.post(
            f'/applications/{slug}/resume/apply',
            json={
                'target_section': 'Education',
                'target_file': 'education.yml',
                'new_content': new_content,
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data['applied'] is True
    assert data['page_count'] == 1
    assert data['render'] == 'ok'

    # Verify file was written and is valid YAML
    app_dir = tmp_path / slug
    written_path = app_dir / 'documents' / 'education.yml'
    assert written_path.exists()
    parsed = yaml.safe_load(written_path.read_text())
    assert isinstance(parsed, list)
    assert parsed[0]['school'] == 'University of Chicago'


def test_apply_resume_invalid_yaml(client):
    """Invalid YAML → 422 invalid_yaml."""
    test_client, (app, tmp_path) = client
    slug = "test-app"

    invalid_yaml = "{ invalid: yaml: content [[[[]"

    response = test_client.post(
        f'/applications/{slug}/resume/apply',
        json={
            'target_section': 'Education',
            'target_file': 'education.yml',
            'new_content': invalid_yaml,
        },
    )

    assert response.status_code == 422
    data = response.json()
    assert data['applied'] is False
    assert data['reason'] == 'invalid_yaml'
    assert 'detail' in data


def test_apply_resume_schema_invalid_education(client):
    """Education schema violation (e.g., item not a dict) → 422 schema_invalid."""
    test_client, (app, tmp_path) = client
    slug = "test-app"

    # Valid YAML but violates education schema (list items must be dicts)
    invalid_schema = """
- "not a dict, should be mapping"
- school: "OK"
"""

    response = test_client.post(
        f'/applications/{slug}/resume/apply',
        json={
            'target_section': 'Education',
            'target_file': 'education.yml',
            'new_content': invalid_schema,
        },
    )

    assert response.status_code == 422
    data = response.json()
    assert data['applied'] is False
    assert data['reason'] == 'schema_invalid'
    assert 'detail' in data


def test_apply_resume_disallowed_target_file(client):
    """Disallowed target_file → 422."""
    test_client, (app, tmp_path) = client
    slug = "test-app"

    response = test_client.post(
        f'/applications/{slug}/resume/apply',
        json={
            'target_section': 'Nope',
            'target_file': 'resume.pdf',  # Not allowed!
            'new_content': 'anything',
        },
    )

    assert response.status_code == 422
    data = response.json()
    assert 'Invalid target_file' in data['detail']


def test_apply_resume_page_count_off_rollback(client):
    """Page count != 1 → 422 page_count_off, file rolled back."""
    test_client, (app, tmp_path) = client
    slug = "test-app"
    app_dir = tmp_path / slug

    # Pre-populate an old education.yml
    documents_dir = app_dir / 'documents'
    documents_dir.mkdir(parents=True, exist_ok=True)
    old_content = "- school: Old School\n  degree: BS\n"
    education_path = documents_dir / 'education.yml'
    education_path.write_text(old_content)

    new_content = "- school: New School\n  degree: PhD\n  field: Physics\n"

    # Mock render to return page_count=2 (failure)
    with patch('jobsmith.api.applications._render_resume', return_value=('ok', 2)):
        response = test_client.post(
            f'/applications/{slug}/resume/apply',
            json={
                'target_section': 'Education',
                'target_file': 'education.yml',
                'new_content': new_content,
            },
        )

    assert response.status_code == 422
    data = response.json()
    assert data['applied'] is False
    assert data['reason'] == 'page_count_off'
    assert data['page_count'] == 2

    # Verify file was rolled back
    assert education_path.read_text() == old_content


def test_apply_resume_page_count_off_rollback_delete_on_no_prior(client):
    """Page count off with no prior file → file deleted on rollback."""
    test_client, (app, tmp_path) = client
    slug = "test-app"
    app_dir = tmp_path / slug

    documents_dir = app_dir / 'documents'
    documents_dir.mkdir(parents=True, exist_ok=True)

    new_content = "- school: New School\n  degree: PhD\n"

    # Mock render to return page_count=2
    with patch('jobsmith.api.applications._render_resume', return_value=('ok', 2)):
        response = test_client.post(
            f'/applications/{slug}/resume/apply',
            json={
                'target_section': 'Education',
                'target_file': 'education.yml',
                'new_content': new_content,
            },
        )

    assert response.status_code == 422
    assert response.json()['reason'] == 'page_count_off'

    # File should not exist after rollback (since there was none before)
    education_path = documents_dir / 'education.yml'
    assert not education_path.exists()


def test_apply_resume_symlink_copy_on_write(client):
    """Symlink is replaced with real file on write."""
    test_client, (app, tmp_path) = client
    slug = "test-app"
    app_dir = tmp_path / slug
    documents_dir = app_dir / 'documents'
    documents_dir.mkdir(parents=True, exist_ok=True)

    # Create a master file to symlink to
    master_dir = tmp_path / 'master'
    master_dir.mkdir(exist_ok=True)
    master_file = master_dir / 'education.yml'
    master_file.write_text("- school: Master\n  degree: BS\n")

    # Create symlink in documents
    education_path = documents_dir / 'education.yml'
    education_path.symlink_to(master_file)
    assert education_path.is_symlink()

    new_content = "- school: Per-App Copy\n  degree: PhD\n"

    with patch('jobsmith.api.applications._render_resume', return_value=('ok', 1)):
        response = test_client.post(
            f'/applications/{slug}/resume/apply',
            json={
                'target_section': 'Education',
                'target_file': 'education.yml',
                'new_content': new_content,
            },
        )

    assert response.status_code == 200

    # Symlink should be replaced with real file
    assert not education_path.is_symlink()
    assert education_path.exists()
    content = education_path.read_text()
    assert 'Per-App Copy' in content


def test_apply_resume_empty_content_rejected(client):
    """Empty new_content → 422."""
    test_client, (app, tmp_path) = client
    slug = "test-app"

    response = test_client.post(
        f'/applications/{slug}/resume/apply',
        json={
            'target_section': 'Education',
            'target_file': 'education.yml',
            'new_content': '   ',  # Whitespace only
        },
    )

    assert response.status_code == 422
    assert 'non-empty' in response.json()['detail']


def test_apply_resume_no_app_dir(client):
    """No application directory → 404."""
    test_client, (app, tmp_path) = client

    # Patch _get_app_dir to return None
    with patch('jobsmith.api.applications._get_app_dir', return_value=None):
        response = test_client.post(
            '/applications/nonexistent/resume/apply',
            json={
                'target_section': 'Education',
                'target_file': 'education.yml',
                'new_content': '- school: Test\n',
            },
        )

    assert response.status_code == 404
    assert 'No application directory' in response.json()['detail']


def test_apply_resume_yaml_reserialized(client):
    """YAML is re-serialized before writing (never raw LLM string)."""
    test_client, (app, tmp_path) = client
    slug = "test-app"

    # Input with unusual formatting
    new_content = """
-    school:   "Univ"
     degree: "BS"
"""

    with patch('jobsmith.api.applications._render_resume', return_value=('ok', 1)):
        response = test_client.post(
            f'/applications/{slug}/resume/apply',
            json={
                'target_section': 'Education',
                'target_file': 'education.yml',
                'new_content': new_content,
            },
        )

    assert response.status_code == 200

    # Read the written file and verify it's proper YAML
    app_dir = tmp_path / slug
    written_path = app_dir / 'documents' / 'education.yml'
    content = written_path.read_text()
    parsed = yaml.safe_load(content)
    assert isinstance(parsed, list)
    assert parsed[0]['school'] == 'Univ'
