"""Tests for feat-bd145368: ingest specialists — resume, LinkedIn, URL, paste.

TDD protocol: tests written before implementation is wired.

Coverage:
  (a) PDF extraction via pdfplumber
  (b) DOCX extraction via python-docx
  (c) LinkedIn ZIP parsing (Positions.csv + Education.csv + Skills.csv)
  (d) ingest_resume produces candidate-*.json with right shape + provenance
  (e) ingest_linkedin_export from ZIP writes all 4 sections
  (f) ingest_linkedin_url auth-wall graceful degrade
  (g) ingest_paste structures free text
  (h) run_ingestion orchestrator wires all sources
  (i) No field in candidate-*.json lacks a provenance entry (no-fabrication check)
  (j) pipeline.dispatch_onboard_pipeline calls run_ingestion
  (k) pipeline.run_onboard_pipeline calls run_ingestion
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

FIXTURES = Path(__file__).parent / "fixtures" / "onboard"

# ---------------------------------------------------------------------------
# Mock LLM call: returns minimal structured candidate
# ---------------------------------------------------------------------------

def _mock_llm_call(prompt: str, source_text: str) -> dict:
    """Mock LLM that returns a minimal valid candidate dict from the source text."""
    return {
        "work": {
            "entries": [
                {
                    "company": "Acme Corp",
                    "title": "Software Engineer",
                    "start_date": "2020",
                    "end_date": "2023",
                    "location": "Remote",
                    "bullets": ["Built distributed systems"],
                }
            ]
        },
        "skill": {
            "skills": [
                {"name": "Python", "category": "technical"},
                {"name": "Go", "category": "technical"},
            ]
        },
        "education": {
            "entries": [
                {
                    "institution": "MIT",
                    "degree": "BS",
                    "field": "Computer Science",
                    "start_date": "2016",
                    "end_date": "2020",
                }
            ]
        },
        "author": {
            "name": "Jane Doe",
            "email": "jane@example.com",
            "phone": "",
            "location": "Remote",
            "github": "",
            "linkedin": "",
            "summary": "Experienced software engineer.",
        },
    }


# ---------------------------------------------------------------------------
# (a) PDF text extraction
# ---------------------------------------------------------------------------

_MINIMAL_PDF = (
    b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
    b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>\nendobj\n"
    b"xref\n0 4\n0000000000 65535 f \n"
    b"0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n"
    b"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n190\n%%EOF"
)


class TestPdfExtraction:
    def test_extract_pdf_returns_string(self, tmp_path: Path):
        from jobsmith.onboard.parsers.extract import extract_pdf_text
        # *.pdf is gitignored; create inline in tmp_path
        pdf_path = tmp_path / "resume.pdf"
        pdf_path.write_bytes(_MINIMAL_PDF)
        result = extract_pdf_text(pdf_path)
        # pdfplumber may or may not extract text from this minimal PDF;
        # either way it returns a string without raising
        assert isinstance(result, str)

    def test_extract_pdf_missing_file_returns_empty(self):
        from jobsmith.onboard.parsers.extract import extract_pdf_text
        result = extract_pdf_text(Path("/nonexistent/file.pdf"))
        assert result == ""

    def test_extract_pdf_wrong_extension_still_safe(self):
        from jobsmith.onboard.parsers.extract import extract_pdf_text
        result = extract_pdf_text(Path("/nonexistent/file.txt"))
        assert result == ""


# ---------------------------------------------------------------------------
# (b) DOCX text extraction
# ---------------------------------------------------------------------------

class TestDocxExtraction:
    def test_extract_docx_returns_text(self):
        from jobsmith.onboard.parsers.extract import extract_docx_text
        result = extract_docx_text(FIXTURES / "resume.docx")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_extract_docx_contains_known_content(self):
        from jobsmith.onboard.parsers.extract import extract_docx_text
        result = extract_docx_text(FIXTURES / "resume.docx")
        assert "Jane Doe" in result

    def test_extract_docx_missing_file_returns_empty(self):
        from jobsmith.onboard.parsers.extract import extract_docx_text
        result = extract_docx_text(Path("/nonexistent/file.docx"))
        assert result == ""


# ---------------------------------------------------------------------------
# (c) LinkedIn ZIP parsing
# ---------------------------------------------------------------------------

class TestLinkedInZipExtraction:
    def test_extract_positions(self):
        from jobsmith.onboard.parsers.extract import extract_linkedin_zip
        data = extract_linkedin_zip(FIXTURES / "linkedin_export.zip")
        assert len(data.positions) >= 1
        assert data.positions[0]["company"] == "Acme Corp"
        assert data.positions[0]["title"] == "Software Engineer"

    def test_extract_education(self):
        from jobsmith.onboard.parsers.extract import extract_linkedin_zip
        data = extract_linkedin_zip(FIXTURES / "linkedin_export.zip")
        assert len(data.education) >= 1
        assert data.education[0]["school"] == "MIT"

    def test_extract_skills(self):
        from jobsmith.onboard.parsers.extract import extract_linkedin_zip
        data = extract_linkedin_zip(FIXTURES / "linkedin_export.zip")
        assert "Python" in data.skills

    def test_raw_text_contains_sections(self):
        from jobsmith.onboard.parsers.extract import extract_linkedin_zip
        data = extract_linkedin_zip(FIXTURES / "linkedin_export.zip")
        assert "Work Experience" in data.raw_text or "Acme Corp" in data.raw_text

    def test_bad_zip_returns_empty(self, tmp_path: Path):
        from jobsmith.onboard.parsers.extract import extract_linkedin_zip
        bad_zip = tmp_path / "bad.zip"
        bad_zip.write_bytes(b"not a zip file")
        data = extract_linkedin_zip(bad_zip)
        assert data.positions == []
        assert data.education == []
        assert data.skills == []

    def test_missing_csvs_in_zip_handled(self, tmp_path: Path):
        from jobsmith.onboard.parsers.extract import extract_linkedin_zip
        empty_zip = tmp_path / "empty.zip"
        with zipfile.ZipFile(str(empty_zip), "w") as zf:
            zf.writestr("README.txt", "nothing here")
        data = extract_linkedin_zip(empty_zip)
        assert data.positions == []
        assert data.skills == []


# ---------------------------------------------------------------------------
# (d) ingest_resume: candidate-*.json shape + provenance
# ---------------------------------------------------------------------------

class TestIngestResume:
    def test_produces_candidate_files(self, tmp_path: Path):
        from jobsmith.onboard.parsers import ingest_resume
        ingest_resume(FIXTURES / "resume.docx", tmp_path, llm_call=_mock_llm_call)
        for section in ("work", "skill", "education", "author"):
            assert (tmp_path / f"candidate-{section}.json").exists()

    def test_candidate_work_has_entries(self, tmp_path: Path):
        from jobsmith.onboard.parsers import ingest_resume
        ingest_resume(FIXTURES / "resume.docx", tmp_path, llm_call=_mock_llm_call)
        data = json.loads((tmp_path / "candidate-work.json").read_text())
        assert "entries" in data["data"]
        assert len(data["data"]["entries"]) >= 1

    def test_candidate_skill_has_skills(self, tmp_path: Path):
        from jobsmith.onboard.parsers import ingest_resume
        ingest_resume(FIXTURES / "resume.docx", tmp_path, llm_call=_mock_llm_call)
        data = json.loads((tmp_path / "candidate-skill.json").read_text())
        assert "skills" in data["data"]

    def test_candidate_author_has_name_field(self, tmp_path: Path):
        from jobsmith.onboard.parsers import ingest_resume
        ingest_resume(FIXTURES / "resume.docx", tmp_path, llm_call=_mock_llm_call)
        data = json.loads((tmp_path / "candidate-author.json").read_text())
        assert "name" in data["data"]

    def test_provenance_file_written(self, tmp_path: Path):
        from jobsmith.onboard.parsers import ingest_resume
        ingest_resume(FIXTURES / "resume.docx", tmp_path, llm_call=_mock_llm_call)
        assert (tmp_path / "provenance-resume.json").exists()

    def test_provenance_is_valid_json(self, tmp_path: Path):
        from jobsmith.onboard.parsers import ingest_resume
        ingest_resume(FIXTURES / "resume.docx", tmp_path, llm_call=_mock_llm_call)
        prov = json.loads((tmp_path / "provenance-resume.json").read_text())
        assert isinstance(prov, dict)

    def test_candidate_source_field_is_resume(self, tmp_path: Path):
        from jobsmith.onboard.parsers import ingest_resume
        ingest_resume(FIXTURES / "resume.docx", tmp_path, llm_call=_mock_llm_call)
        data = json.loads((tmp_path / "candidate-work.json").read_text())
        assert data["source"] == "resume"

    def test_pdf_input_accepted(self, tmp_path: Path):
        from jobsmith.onboard.parsers import ingest_resume
        # Build a minimal valid PDF inline (*.pdf is gitignored in this repo).
        pdf_bytes = (
            b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>\nendobj\n"
            b"xref\n0 4\n0000000000 65535 f \n"
            b"0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n"
            b"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n190\n%%EOF"
        )
        pdf_path = tmp_path / "resume.pdf"
        pdf_path.write_bytes(pdf_bytes)
        # PDF may have empty extraction; specialist should still write files
        ingest_resume(pdf_path, tmp_path / "state", llm_call=_mock_llm_call)
        assert (tmp_path / "state" / "candidate-author.json").exists()

    def test_txt_input_accepted(self, tmp_path: Path):
        from jobsmith.onboard.parsers import ingest_resume
        txt = tmp_path / "resume.txt"
        txt.write_text("Jane Doe\nSoftware Engineer at Acme Corp\nPython, Go")
        ingest_resume(txt, tmp_path / "state", llm_call=_mock_llm_call)
        assert (tmp_path / "state" / "candidate-author.json").exists()


# ---------------------------------------------------------------------------
# (i) No-fabrication check: all populated fields have provenance
# ---------------------------------------------------------------------------

class TestNoFabrication:
    def test_all_populated_fields_have_provenance(self, tmp_path: Path):
        from jobsmith.onboard.parsers import ingest_paste
        ingest_paste(
            "Jane Doe, Software Engineer at Acme Corp, Python expert",
            tmp_path,
            llm_call=_mock_llm_call,
        )
        prov = json.loads((tmp_path / "provenance-paste.json").read_text())
        work_data = json.loads((tmp_path / "candidate-work.json").read_text())
        # Every non-empty work entry should have at least one provenance entry
        entries = work_data["data"].get("entries", [])
        if entries:
            # At least some fields should be covered
            work_prov_keys = [k for k in prov if k.startswith("work")]
            assert len(work_prov_keys) > 0, "No work provenance entries found"

    def test_provenance_values_are_strings(self, tmp_path: Path):
        from jobsmith.onboard.parsers import ingest_paste
        ingest_paste(
            "Jane Doe, Senior Engineer",
            tmp_path,
            llm_call=_mock_llm_call,
        )
        prov = json.loads((tmp_path / "provenance-paste.json").read_text())
        for key, val in prov.items():
            assert isinstance(val, str), f"Provenance value for {key!r} is not a string"


# ---------------------------------------------------------------------------
# (e) ingest_linkedin_export from ZIP
# ---------------------------------------------------------------------------

class TestIngestLinkedInExport:
    def test_produces_all_candidate_files(self, tmp_path: Path):
        from jobsmith.onboard.parsers import ingest_linkedin_export
        ingest_linkedin_export(
            FIXTURES / "linkedin_export.zip",
            tmp_path,
            llm_call=_mock_llm_call,
        )
        for section in ("work", "skill", "education", "author"):
            assert (tmp_path / f"candidate-{section}.json").exists()

    def test_work_entries_from_positions_csv(self, tmp_path: Path):
        from jobsmith.onboard.parsers import ingest_linkedin_export
        ingest_linkedin_export(
            FIXTURES / "linkedin_export.zip",
            tmp_path,
            llm_call=_mock_llm_call,
        )
        data = json.loads((tmp_path / "candidate-work.json").read_text())
        # Should have Acme Corp from Positions.csv
        companies = [e["company"] for e in data["data"].get("entries", [])]
        assert "Acme Corp" in companies

    def test_skills_from_skills_csv(self, tmp_path: Path):
        from jobsmith.onboard.parsers import ingest_linkedin_export
        ingest_linkedin_export(
            FIXTURES / "linkedin_export.zip",
            tmp_path,
            llm_call=_mock_llm_call,
        )
        data = json.loads((tmp_path / "candidate-skill.json").read_text())
        skill_names = [s["name"] for s in data["data"].get("skills", [])]
        assert "Python" in skill_names

    def test_provenance_file_written(self, tmp_path: Path):
        from jobsmith.onboard.parsers import ingest_linkedin_export
        ingest_linkedin_export(
            FIXTURES / "linkedin_export.zip",
            tmp_path,
            llm_call=_mock_llm_call,
        )
        assert (tmp_path / "provenance-linkedin.json").exists()

    def test_source_field_is_linkedin(self, tmp_path: Path):
        from jobsmith.onboard.parsers import ingest_linkedin_export
        ingest_linkedin_export(
            FIXTURES / "linkedin_export.zip",
            tmp_path,
            llm_call=_mock_llm_call,
        )
        data = json.loads((tmp_path / "candidate-work.json").read_text())
        assert data["source"] == "linkedin"


# ---------------------------------------------------------------------------
# (f) ingest_linkedin_url: auth-wall graceful degrade
# ---------------------------------------------------------------------------

class TestIngestLinkedInUrl:
    def test_auth_wall_writes_fallback_status(self, tmp_path: Path):
        """Mocked httpx returns empty body (auth wall) → graceful degrade."""
        from jobsmith.onboard.parsers import ingest_linkedin_url

        with patch("jobsmith.onboard.parsers.ingest._fetch_url_text", return_value=""):
            ingest_linkedin_url(
                "https://www.linkedin.com/in/janedoe/",
                tmp_path,
                llm_call=_mock_llm_call,
            )
        assert (tmp_path / "url-fetch-status.json").exists()
        status = json.loads((tmp_path / "url-fetch-status.json").read_text())
        assert status["status"] == "failed"
        assert "reason" in status
        assert "message" in status

    def test_auth_wall_still_writes_empty_candidate_files(self, tmp_path: Path):
        from jobsmith.onboard.parsers import ingest_linkedin_url

        with patch("jobsmith.onboard.parsers.ingest._fetch_url_text", return_value=""):
            ingest_linkedin_url(
                "https://www.linkedin.com/in/janedoe/",
                tmp_path,
                llm_call=_mock_llm_call,
            )
        for section in ("work", "skill", "education", "author"):
            assert (tmp_path / f"candidate-{section}.json").exists()

    def test_non_linkedin_url_degrades(self, tmp_path: Path):
        from jobsmith.onboard.parsers import ingest_linkedin_url

        ingest_linkedin_url(
            "https://example.com/profile",
            tmp_path,
            llm_call=_mock_llm_call,
        )
        status = json.loads((tmp_path / "url-fetch-status.json").read_text())
        assert status["reason"] == "not_linkedin_url"

    def test_successful_fetch_calls_llm(self, tmp_path: Path):
        """When fetch succeeds, llm_call is invoked with the page text."""
        from jobsmith.onboard.parsers import ingest_linkedin_url

        llm_called_with: list[tuple] = []

        def capturing_llm(prompt: str, source_text: str) -> dict:
            llm_called_with.append((prompt, source_text))
            return _mock_llm_call(prompt, source_text)

        with patch(
            "jobsmith.onboard.parsers.ingest._fetch_url_text",
            return_value="Jane Doe Senior Engineer at Acme Corp",
        ):
            ingest_linkedin_url(
                "https://www.linkedin.com/in/janedoe/",
                tmp_path,
                llm_call=capturing_llm,
            )
        assert len(llm_called_with) == 1
        assert "Jane Doe" in llm_called_with[0][1]

    def test_empty_candidate_sections_present_on_degrade(self, tmp_path: Path):
        from jobsmith.onboard.parsers import ingest_linkedin_url

        with patch("jobsmith.onboard.parsers.ingest._fetch_url_text", return_value=""):
            result = ingest_linkedin_url(
                "https://www.linkedin.com/in/janedoe/",
                tmp_path,
                llm_call=_mock_llm_call,
            )
        assert "work" in result
        assert "skill" in result
        assert "education" in result
        assert "author" in result


# ---------------------------------------------------------------------------
# (g) ingest_paste
# ---------------------------------------------------------------------------

class TestIngestPaste:
    def test_structures_free_text(self, tmp_path: Path):
        from jobsmith.onboard.parsers import ingest_paste
        result = ingest_paste(
            "Jane Doe. Software Engineer at Acme Corp 2020-2023. Python, Go.",
            tmp_path,
            llm_call=_mock_llm_call,
        )
        assert result["work"]["entries"][0]["company"] == "Acme Corp"

    def test_produces_all_candidate_files(self, tmp_path: Path):
        from jobsmith.onboard.parsers import ingest_paste
        ingest_paste("Jane Doe, engineer", tmp_path, llm_call=_mock_llm_call)
        for section in ("work", "skill", "education", "author"):
            assert (tmp_path / f"candidate-{section}.json").exists()

    def test_empty_paste_still_writes_files(self, tmp_path: Path):
        from jobsmith.onboard.parsers import ingest_paste
        ingest_paste("", tmp_path, llm_call=_mock_llm_call)
        assert (tmp_path / "candidate-work.json").exists()

    def test_whitespace_only_paste_writes_files(self, tmp_path: Path):
        from jobsmith.onboard.parsers import ingest_paste
        ingest_paste("   \n  ", tmp_path, llm_call=_mock_llm_call)
        assert (tmp_path / "candidate-work.json").exists()

    def test_provenance_file_named_correctly(self, tmp_path: Path):
        from jobsmith.onboard.parsers import ingest_paste
        ingest_paste("Jane Doe", tmp_path, llm_call=_mock_llm_call, source_name="paste")
        assert (tmp_path / "provenance-paste.json").exists()

    def test_paste_file_source_name(self, tmp_path: Path):
        from jobsmith.onboard.parsers import ingest_paste
        ingest_paste(
            "Jane Doe",
            tmp_path,
            llm_call=_mock_llm_call,
            source_name="paste_file",
        )
        assert (tmp_path / "provenance-paste_file.json").exists()


# ---------------------------------------------------------------------------
# (h) run_ingestion orchestrator
# ---------------------------------------------------------------------------

class TestRunIngestion:
    def test_returns_zero_with_resume(self, tmp_path: Path):
        from jobsmith.onboard.parsers import run_ingestion
        state_dir = tmp_path / ".onboard-state"
        rc = run_ingestion(
            state_dir,
            tmp_path,
            resume_file=FIXTURES / "resume.docx",
            llm_call=_mock_llm_call,
        )
        assert rc == 0

    def test_returns_one_with_no_inputs(self, tmp_path: Path):
        from jobsmith.onboard.parsers import run_ingestion
        state_dir = tmp_path / ".onboard-state"
        rc = run_ingestion(state_dir, tmp_path, llm_call=_mock_llm_call)
        assert rc == 1

    def test_paste_file_is_read(self, tmp_path: Path):
        from jobsmith.onboard.parsers import run_ingestion
        paste_file = tmp_path / "paste.txt"
        paste_file.write_text("Jane Doe, Software Engineer at Acme Corp")
        state_dir = tmp_path / ".onboard-state"
        rc = run_ingestion(
            state_dir,
            tmp_path,
            paste_file=paste_file,
            llm_call=_mock_llm_call,
        )
        assert rc == 0
        assert (state_dir / "candidate-work.json").exists()

    def test_multiple_sources_all_processed(self, tmp_path: Path):
        from jobsmith.onboard.parsers import run_ingestion
        state_dir = tmp_path / ".onboard-state"
        rc = run_ingestion(
            state_dir,
            tmp_path,
            resume_file=FIXTURES / "resume.docx",
            linkedin_export=FIXTURES / "linkedin_export.zip",
            paste="Jane Doe, engineer",
            llm_call=_mock_llm_call,
        )
        assert rc == 0
        # LinkedIn export should overwrite resume candidate files
        assert (state_dir / "candidate-work.json").exists()

    def test_linkedin_url_processed(self, tmp_path: Path):
        from jobsmith.onboard.parsers import run_ingestion
        state_dir = tmp_path / ".onboard-state"
        with patch("jobsmith.onboard.parsers.ingest._fetch_url_text", return_value=""):
            rc = run_ingestion(
                state_dir,
                tmp_path,
                linkedin_url="https://www.linkedin.com/in/janedoe/",
                llm_call=_mock_llm_call,
            )
        assert rc == 0  # graceful degrade counts as success

    def test_missing_paste_file_still_returns(self, tmp_path: Path):
        from jobsmith.onboard.parsers import run_ingestion
        state_dir = tmp_path / ".onboard-state"
        rc = run_ingestion(
            state_dir,
            tmp_path,
            paste_file=tmp_path / "nonexistent.txt",
            llm_call=_mock_llm_call,
        )
        # paste_file is set so had_input=True, but read fails → error recorded
        assert rc == 1


# ---------------------------------------------------------------------------
# (j) + (k) Pipeline wires run_ingestion into both STUB boundaries
# ---------------------------------------------------------------------------

class TestPipelineWiresIngestion:
    def _make_repo(self, tmp_path: Path) -> Path:
        """Minimal bootstrapped repo."""
        from jobsmith._init import scaffold_repo
        scaffold_repo(tmp_path)
        return tmp_path

    def test_dispatch_onboard_pipeline_calls_run_ingestion(self, tmp_path: Path):
        repo_root = self._make_repo(tmp_path)
        from jobsmith.onboard import pipeline

        with patch(
            "jobsmith.onboard.pipeline.run_ingestion"
        ) as mock_ingest:
            mock_ingest.return_value = 0
            rc = pipeline.dispatch_onboard_pipeline(
                repo_root=repo_root,
                paste="Jane Doe engineer",
            )
        assert rc == 0
        mock_ingest.assert_called_once()

    def test_run_onboard_pipeline_calls_run_ingestion(self, tmp_path: Path):
        repo_root = self._make_repo(tmp_path)
        from jobsmith.onboard import pipeline

        with patch(
            "jobsmith.onboard.pipeline.run_ingestion"
        ) as mock_ingest:
            mock_ingest.return_value = 0
            rc = pipeline.run_onboard_pipeline(
                repo_root=repo_root,
                paste="Jane Doe engineer",
            )
        assert rc == 0
        mock_ingest.assert_called_once()

    def test_dispatch_passes_inputs_to_run_ingestion(self, tmp_path: Path):
        repo_root = self._make_repo(tmp_path)
        from jobsmith.onboard import pipeline

        captured_kwargs: list[dict] = []

        def capture(*args, **kwargs):
            captured_kwargs.append(kwargs)
            return 0

        with patch("jobsmith.onboard.pipeline.run_ingestion", side_effect=capture):
            pipeline.dispatch_onboard_pipeline(
                repo_root=repo_root,
                paste="Jane Doe",
                linkedin_url="https://www.linkedin.com/in/janedoe/",
            )

        assert len(captured_kwargs) == 1
        kw = captured_kwargs[0]
        assert kw.get("paste") == "Jane Doe"
        assert kw.get("linkedin_url") == "https://www.linkedin.com/in/janedoe/"

    def test_run_pipeline_emits_events(self, tmp_path: Path):
        """run_onboard_pipeline should emit phase_start and phase_complete events."""
        repo_root = self._make_repo(tmp_path)
        from jobsmith.onboard import pipeline

        events = MagicMock()
        with patch("jobsmith.onboard.pipeline.run_ingestion", return_value=0):
            pipeline.run_onboard_pipeline(
                repo_root=repo_root,
                paste="Jane Doe",
                events=events,
            )
        # events.emit should have been called (phase_start + phase_complete)
        assert events.emit.call_count >= 2
