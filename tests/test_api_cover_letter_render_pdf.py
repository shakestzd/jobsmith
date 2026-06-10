"""Tests for POST /api/applications/{slug}/cover-letter/render-pdf (feat-0e29138c).

On-demand cover-letter PDF generation:
- 404 when no draft exists;
- the generated cover-letter.qmd is a self-contained standalone-typst doc that
  embeds the draft body + a contact header read from author.yml;
- render is quarto-availability-aware: if quarto is on PATH, a non-trivial PDF is
  produced and the response is ``{rendered: true, path: "cover-letter.pdf"}``;
  if quarto is absent, the endpoint returns ``{rendered: false,
  reason: "quarto_not_available"}`` rather than 500.
- draft headers (H1 + contact + RE block) are stripped before embedding;
  body-only drafts pass through unchanged;
- the qmd uses the awesomecv accent colour (#262F99) not the old grey (#555/#666).
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobsmith.api.applications import (
    _build_cover_letter_qmd,
    _load_letter_author,
    _typst_escape,
)
from jobsmith.api.applications import (
    router as applications_router,
)

# ---------------------------------------------------------------------------
# Sample cover-letter with a full standalone header (H1 + contact + RE block).
# This is the format produced by some pipeline drafts and must be stripped to
# salutation-onwards before embedding in the qmd.
# ---------------------------------------------------------------------------
_FULL_HEADER_DRAFT = """\
# Cover Letter — Acme Corp, Software Engineer

Thando Dlamini
thando@example.com · Durham, NC

June 2025

**Hiring Team**
Acme Corp
**RE:** Software Engineer

Dear Hiring Team,

I am an excellent fit for this role.

Sincerely,
Thando
"""

_BODY_ONLY_DRAFT = """\
Dear Hiring Team,

I am an excellent fit for this role.

Sincerely,
Thando
"""


class _DummyConn:
    def close(self) -> None:  # pragma: no cover - trivial
        pass


@pytest.fixture()
def render_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, Path, str]:
    slug = "acme-swe-2025"
    app_dir = tmp_path / "applications" / slug
    docs_dir = app_dir / "documents"
    docs_dir.mkdir(parents=True)

    cl_path = app_dir / "cover-letter-draft.md"
    cl_path.write_text(
        "Hello,\n\nI am a great fit for this role.\n\nShakes Dlamini\n",
        encoding="utf-8",
    )
    (docs_dir / "author.yml").write_text(
        "author:\n"
        "  firstname: Thando\n"
        "  lastname: Dlamini\n"
        "  position: Data Analyst\n"
        "  contacts:\n"
        "    - icon: fa location-crosshairs\n"
        "      text: Durham, NC\n"
        "    - icon: fa envelope\n"
        "      text: shakestzd@gmail.com\n",
        encoding="utf-8",
    )

    import jobsmith.api.applications as appmod

    monkeypatch.setattr(appmod, "_resolve_cover_letter", lambda s, conn: cl_path)
    monkeypatch.setattr(appmod, "_resolve_docs_dir", lambda s, conn: docs_dir)
    monkeypatch.setattr(appmod, "_get_app_dir", lambda s: app_dir)
    monkeypatch.setattr(appmod, "_get_db_path", lambda: tmp_path / "jobsmith.db")
    monkeypatch.setattr(appmod, "_open_conn", lambda p: _DummyConn())
    # No DB → no run row, so job_company/job_position default to None (graceful).
    monkeypatch.setattr(appmod, "_find_run_row_for_slug", lambda conn, slug: None)

    app = FastAPI()
    app.include_router(applications_router, prefix="/api")
    return TestClient(app, raise_server_exceptions=True), docs_dir, slug


class TestTemplate:
    def test_typst_escape_quotes_and_backslashes(self) -> None:
        assert _typst_escape('a"b\\c') == 'a\\"b\\\\c'

    def test_author_read_from_author_yml(self, tmp_path: Path) -> None:
        docs = tmp_path / "documents"
        docs.mkdir()
        (docs / "author.yml").write_text(
            "author:\n"
            "  firstname: Thando\n"
            "  lastname: Dlamini\n"
            "  position: Analyst\n"
            "  contacts:\n"
            "    - icon: fa envelope\n"
            "      text: x@y.com\n",
            encoding="utf-8",
        )
        author = _load_letter_author(docs)
        assert author["name"] == "Thando Dlamini"
        assert author["position"] == "Analyst"
        assert author["email"] == "x@y.com"

    def test_qmd_is_standalone_typst_and_embeds_body(self) -> None:
        author = {
            "name": "Thando Dlamini",
            "position": "Analyst",
            "location": "Durham, NC",
            "email": "x@y.com",
            "phone": "",
            "github": "shakestzd",
            "linkedin": "",
        }
        qmd = _build_cover_letter_qmd("Hello,\n\nMy body.\n", author)
        assert "format:\n  typst:" in qmd
        assert "awesomecv" not in qmd  # standalone, not the resume extension
        assert "My body." in qmd
        # Contact values live inside Typst string literals (#"...") so "@"/"."
        # do not parse as label/reference syntax.
        assert '#"x@y.com' in qmd or 'x@y.com' in qmd
        assert "github.com/shakestzd" in qmd

    def test_qmd_uses_awesomecv_accent_colour_not_grey(self) -> None:
        """Header must use #262F99 (accent) and #131A28 (name) — not generic grey."""
        author = {
            "name": "Thando Dlamini",
            "position": "Senior Analyst",
            "location": "Durham, NC",
            "email": "x@y.com",
            "phone": "",
            "github": "",
            "linkedin": "",
        }
        qmd = _build_cover_letter_qmd("Dear Team,\n\nBody.\n", author)
        # Must contain awesomecv accent colour
        assert "#262F99" in qmd
        # Must contain awesomecv name dark colour
        assert "#131A28" in qmd
        # Must NOT contain the old grey values
        assert "#555" not in qmd
        assert "#666" not in qmd


class TestDraftHeaderStripping:
    """The PDF path must strip pre-salutation content from the draft."""

    _AUTHOR = {
        "name": "Thando Dlamini",
        "position": "Analyst",
        "location": "Durham, NC",
        "email": "x@y.com",
        "phone": "",
        "github": "",
        "linkedin": "",
    }

    def test_full_header_draft_strips_to_salutation(self) -> None:
        """H1 + contact + RE block must not appear in the qmd body."""
        from jobsmith.assemble import _extract_letter_body

        body = _extract_letter_body(_FULL_HEADER_DRAFT)
        qmd = _build_cover_letter_qmd(body, self._AUTHOR)

        # The H1 markdown header must not appear verbatim in the qmd
        assert "# Cover Letter" not in qmd
        # A second contact line must not appear in the markdown body section
        # (the template header has its own contact line injected as Typst)
        assert "thando@example.com · Durham, NC" not in qmd
        # The **RE:** block must not appear
        assert "**RE:**" not in qmd
        # The salutation and body paragraphs must be present
        assert "Dear Hiring Team," in qmd
        assert "I am an excellent fit" in qmd

    def test_body_only_draft_passes_through_unchanged(self) -> None:
        """A draft already starting at the salutation must not be altered."""
        from jobsmith.assemble import _extract_letter_body

        body = _extract_letter_body(_BODY_ONLY_DRAFT)
        assert body.strip() == _BODY_ONLY_DRAFT.strip()
        qmd = _build_cover_letter_qmd(body, self._AUTHOR)
        assert "Dear Hiring Team," in qmd
        assert "I am an excellent fit" in qmd

    def test_recipient_block_uses_job_data_not_draft(self) -> None:
        """job_company/job_position compose the recipient block from structured data."""
        from jobsmith.assemble import _extract_letter_body

        body = _extract_letter_body(_FULL_HEADER_DRAFT)
        qmd = _build_cover_letter_qmd(
            body,
            self._AUTHOR,
            job_company="Acme Corp",
            job_position="Software Engineer",
        )
        # Structured recipient block must be present
        assert "Acme Corp" in qmd
        assert "Software Engineer" in qmd
        # The RE: line should come from the template, not the draft body
        assert "**RE:**" not in qmd  # markdown form must be gone

    def test_no_recipient_block_when_job_data_absent(self) -> None:
        """Omit recipient block gracefully when job data is not available."""
        qmd = _build_cover_letter_qmd(
            "Dear Team,\n\nBody.\n",
            self._AUTHOR,
            job_company=None,
            job_position=None,
        )
        assert "Hiring Team" not in qmd


class TestRenderEndpoint:
    def test_404_when_no_draft(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        render_client: tuple[TestClient, Path, str],
    ) -> None:
        client, _docs, slug = render_client
        import jobsmith.api.applications as appmod

        monkeypatch.setattr(appmod, "_resolve_cover_letter", lambda s, conn: None)
        resp = client.post(f"/api/applications/{slug}/cover-letter/render-pdf")
        assert resp.status_code == 404

    def test_render_or_graceful_skip(
        self, render_client: tuple[TestClient, Path, str]
    ) -> None:
        client, docs_dir, slug = render_client
        resp = client.post(f"/api/applications/{slug}/cover-letter/render-pdf")
        assert resp.status_code == 200
        data = resp.json()

        # The qmd is always generated (render is the only quarto-dependent step).
        qmd = docs_dir / "cover-letter.qmd"
        assert qmd.is_file()
        assert "I am a great fit" in qmd.read_text(encoding="utf-8")

        if shutil.which("quarto") is not None:
            # quarto present: assert a non-trivial PDF was produced.
            assert data["rendered"] is True, data
            assert data["path"] == "cover-letter.pdf"
            pdf = docs_dir / "cover-letter.pdf"
            assert pdf.is_file()
            assert pdf.stat().st_size > 10_000
        else:
            # quarto absent: graceful skip, never a 500.
            assert data["rendered"] is False
            assert data["reason"] == "quarto_not_available"
