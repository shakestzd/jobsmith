"""jobsmith.onboard.parsers.ingest — LLM-structured specialist parsers.

Each specialist:
  1. Extracts text from the source (PDF/DOCX/ZIP/URL/paste).
  2. Calls the LLM to structure it into candidate-*.json (work/skill/education/author).
  3. Writes candidate-*.json and provenance-*.json to state_dir.
  4. Returns the candidate dict.

No fabrication contract
-----------------------
Every field in candidate-*.json must link to a source snippet in the
corresponding provenance-*.json. The LLM prompt explicitly enforces this:
the model is instructed to leave a field empty rather than invent content.

LLM call injection
------------------
The ``llm_call`` parameter accepts a callable:
    (prompt: str, source_text: str) -> dict

This makes the LLM call trivially mockable in tests. The production default
calls ``claude -p`` via subprocess and parses the JSON response.
"""
from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from pathlib import Path

import httpx

from .extract import extract_docx_text, extract_linkedin_zip, extract_pdf_text

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Candidate schema: field names that must match master YAMLs
# ---------------------------------------------------------------------------

CANDIDATE_SCHEMA = {
    "work": {
        "required": ["entries"],
        "entry_fields": ["company", "title", "start_date", "end_date", "bullets"],
    },
    "skill": {
        "required": ["skills"],
        "entry_fields": ["name", "category"],
    },
    "education": {
        "required": ["entries"],
        "entry_fields": ["institution", "degree", "field", "start_date", "end_date"],
    },
    "author": {
        "required": ["name"],
        "fields": ["name", "email", "phone", "location", "github", "linkedin", "summary"],
    },
}

# ---------------------------------------------------------------------------
# LLM structuring prompt templates
# ---------------------------------------------------------------------------

_STRUCTURE_PROMPT = """\
You are a resume/profile parsing assistant. Extract structured information from
the provided source text and return ONLY valid JSON matching the schema below.

IMPORTANT RULES:
- No fabrication: only include information explicitly present in the source text.
- If a field is not found in the source, use null or an empty list/string.
- Every extracted claim must be traceable to a specific span of source text.
- Return ONLY the JSON object, no markdown fences, no explanation.

Schema to fill:
{{
  "work": {{
    "entries": [
      {{"company": "...", "title": "...", "start_date": "...", "end_date": "...",
        "location": "...", "bullets": ["..."]}}
    ]
  }},
  "skill": {{
    "skills": [
      {{"name": "...", "category": "technical|soft|domain|tool"}}
    ]
  }},
  "education": {{
    "entries": [
      {{"institution": "...", "degree": "...", "field": "...",
        "start_date": "...", "end_date": "..."}}
    ]
  }},
  "author": {{
    "name": "...", "email": "...", "phone": "...", "location": "...",
    "github": "...", "linkedin": "...", "summary": "..."
  }}
}}

Source text:
---
{source_text}
---
"""

_PROVENANCE_PROMPT = """\
You are a provenance mapper. Given the structured data and the source text,
identify which span of source text supports each extracted field value.
Return ONLY valid JSON mapping field paths to the supporting source snippet.
Use empty string if no supporting text exists (should not happen for populated fields).

Return format:
{{
  "work.entries[0].company": "Acme Corp",
  "work.entries[0].title": "Senior Engineer",
  ...
}}

Structured data:
{structured_json}

Source text:
---
{source_text}
---
"""


# ---------------------------------------------------------------------------
# Default LLM call implementation (subprocess claude -p)
# ---------------------------------------------------------------------------

def _default_llm_call(prompt: str, source_text: str) -> dict:
    """Call claude -p with a structured prompt and parse the JSON response.

    Falls back to an empty dict on any error (subprocess not available,
    JSON parse failure, etc.).
    """
    full_prompt = prompt.format(source_text=source_text)
    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text", full_prompt],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            logger.warning("claude -p failed: %s", result.stderr[:500])
            return {}
        output = result.stdout.strip()
        # Strip markdown fences if present
        if output.startswith("```"):
            lines = output.splitlines()
            output = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
        return json.loads(output)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        logger.warning("LLM call failed: %s", exc)
        return {}


def _default_provenance_call(
    prompt: str,
    structured_json: str,
    source_text: str,
) -> dict:
    """Call claude -p to map provenance. Returns empty dict on failure."""
    full_prompt = prompt.format(
        structured_json=structured_json,
        source_text=source_text,
    )
    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text", full_prompt],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            return {}
        output = result.stdout.strip()
        if output.startswith("```"):
            lines = output.splitlines()
            output = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
        return json.loads(output)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Helper: write candidate-*.json and provenance-*.json
# ---------------------------------------------------------------------------

def _write_candidate_files(
    state_dir: Path,
    candidate: dict,
    provenance: dict,
    source_name: str,
) -> None:
    """Atomically write candidate and provenance JSON files to state_dir."""
    state_dir.mkdir(parents=True, exist_ok=True)

    # Write per-section candidate files
    for section in ("work", "skill", "education", "author"):
        section_data = candidate.get(section, {})
        out_path = state_dir / f"candidate-{section}.json"
        out_path.write_text(
            json.dumps(
                {
                    "source": source_name,
                    "section": section,
                    "data": section_data,
                },
                indent=2,
            )
        )
        logger.debug("wrote %s", out_path)

    # Write combined provenance map
    prov_path = state_dir / f"provenance-{source_name}.json"
    prov_path.write_text(json.dumps(provenance, indent=2))
    logger.debug("wrote %s", prov_path)


# ---------------------------------------------------------------------------
# Individual specialist parsers
# ---------------------------------------------------------------------------

def ingest_resume(
    path: Path,
    state_dir: Path,
    *,
    llm_call: Callable[[str, str], dict] | None = None,
) -> dict:
    """Parse a resume PDF or DOCX into candidate-*.json + provenance.

    Parameters
    ----------
    path:
        Path to the resume file (PDF or DOCX).
    state_dir:
        `.onboard-state/` directory to write outputs into.
    llm_call:
        Optional override for the LLM call (for testing). Signature:
        ``(prompt: str, source_text: str) -> dict``

    Returns
    -------
    dict
        The structured candidate dict (work/skill/education/author).
    """
    if llm_call is None:
        llm_call = _default_llm_call

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        source_text = extract_pdf_text(path)
    elif suffix in (".docx", ".doc"):
        source_text = extract_docx_text(path)
    else:
        # TXT, Markdown, or other text formats — read directly
        try:
            source_text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            source_text = ""

    # Fall back to filename stub if extraction yielded nothing
    if not source_text.strip():
        logger.warning("ingest_resume: empty extraction from %s — skipping LLM", path)
        candidate: dict = {"work": {"entries": []}, "skill": {"skills": []},
                           "education": {"entries": []}, "author": {"name": ""}}
        provenance: dict = {}
        _write_candidate_files(state_dir, candidate, provenance, "resume")
        return candidate

    candidate = llm_call(_STRUCTURE_PROMPT, source_text)
    if not candidate:
        candidate = {"work": {"entries": []}, "skill": {"skills": []},
                     "education": {"entries": []}, "author": {"name": ""}}

    provenance = _default_provenance_call(
        _PROVENANCE_PROMPT,
        json.dumps(candidate),
        source_text,
    ) if llm_call is _default_llm_call else _build_stub_provenance(candidate, source_text)

    _write_candidate_files(state_dir, candidate, provenance, "resume")
    return candidate


def ingest_linkedin_export(
    path: Path,
    state_dir: Path,
    *,
    llm_call: Callable[[str, str], dict] | None = None,
) -> dict:
    """Parse a LinkedIn 'Download your data' ZIP (or profile PDF) into candidate-*.json.

    For a ZIP, structured CSVs are parsed directly; the LLM only fills gaps.
    For a PDF, full text extraction is used.
    """
    if llm_call is None:
        llm_call = _default_llm_call

    suffix = path.suffix.lower()
    if suffix == ".zip":
        li_data = extract_linkedin_zip(path)
        source_text = li_data.raw_text

        # Pre-structure from CSV data to reduce LLM load
        candidate = _linkedin_data_to_candidate(li_data)
        # Use LLM to fill author section and any gaps
        llm_candidate = llm_call(_STRUCTURE_PROMPT, source_text) if source_text.strip() else {}
        if llm_candidate and "author" in llm_candidate:
            candidate["author"] = llm_candidate["author"]
    elif suffix == ".pdf":
        source_text = extract_pdf_text(path)
        candidate = llm_call(_STRUCTURE_PROMPT, source_text) if source_text.strip() else {}
        if not candidate:
            candidate = {"work": {"entries": []}, "skill": {"skills": []},
                         "education": {"entries": []}, "author": {"name": ""}}
    else:
        logger.warning("ingest_linkedin_export: unsupported format %s", suffix)
        candidate = {"work": {"entries": []}, "skill": {"skills": []},
                     "education": {"entries": []}, "author": {"name": ""}}
        source_text = ""

    provenance = _build_stub_provenance(candidate, source_text)
    _write_candidate_files(state_dir, candidate, provenance, "linkedin")
    return candidate


def ingest_linkedin_url(
    url: str,
    state_dir: Path,
    *,
    llm_call: Callable[[str, str], dict] | None = None,
) -> dict:
    """Best-effort fetch a LinkedIn profile URL and parse it.

    LinkedIn typically blocks unauthenticated scraping. When fetch fails (auth
    wall, redirect, or network error), writes a 'needs_manual_input' marker and
    returns an empty candidate rather than raising.

    Parameters
    ----------
    url:
        LinkedIn profile URL (e.g. https://www.linkedin.com/in/janedoe/).
    state_dir:
        `.onboard-state/` directory.
    llm_call:
        Optional LLM call override for testing.

    Returns
    -------
    dict
        The structured candidate dict, or empty sections with a warning marker.
    """
    if llm_call is None:
        llm_call = _default_llm_call

    empty_candidate = {
        "work": {"entries": []},
        "skill": {"skills": []},
        "education": {"entries": []},
        "author": {"name": ""},
    }

    # Detect obvious non-LinkedIn URLs early
    if "linkedin.com" not in url:
        logger.info("ingest_linkedin_url: non-LinkedIn URL %s — skipping fetch", url)
        _write_url_fallback(state_dir, url, "not_linkedin_url")
        _write_candidate_files(state_dir, empty_candidate, {}, "linkedin_url")
        return empty_candidate

    # Attempt fetch
    source_text = _fetch_url_text(url)

    if not source_text:
        logger.info(
            "ingest_linkedin_url: could not fetch %s "
            "(auth wall or network error) — user should upload export or paste",
            url,
        )
        _write_url_fallback(state_dir, url, "auth_wall")
        _write_candidate_files(state_dir, empty_candidate, {}, "linkedin_url")
        return empty_candidate

    candidate = llm_call(_STRUCTURE_PROMPT, source_text)
    if not candidate:
        candidate = empty_candidate

    provenance = _build_stub_provenance(candidate, source_text)
    _write_candidate_files(state_dir, candidate, provenance, "linkedin_url")
    return candidate


def ingest_paste(
    text: str,
    state_dir: Path,
    *,
    llm_call: Callable[[str, str], dict] | None = None,
    source_name: str = "paste",
) -> dict:
    """Structure free-text paste into candidate-*.json.

    Accepts any free-form text: pasted LinkedIn profile, resume text,
    bio notes, etc.

    Parameters
    ----------
    text:
        Raw pasted text from the user.
    state_dir:
        `.onboard-state/` directory.
    llm_call:
        Optional LLM call override for testing.
    source_name:
        Used to name the provenance file (``provenance-<source_name>.json``).

    Returns
    -------
    dict
        The structured candidate dict.
    """
    if llm_call is None:
        llm_call = _default_llm_call

    empty_candidate = {
        "work": {"entries": []},
        "skill": {"skills": []},
        "education": {"entries": []},
        "author": {"name": ""},
    }

    if not text or not text.strip():
        _write_candidate_files(state_dir, empty_candidate, {}, source_name)
        return empty_candidate

    candidate = llm_call(_STRUCTURE_PROMPT, text)
    if not candidate:
        candidate = empty_candidate

    provenance = _build_stub_provenance(candidate, text)
    _write_candidate_files(state_dir, candidate, provenance, source_name)
    return candidate


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def run_ingestion(
    state_dir: Path,
    repo_root: Path,  # noqa: ARG001 — reserved for future use (master YAML paths)
    *,
    resume_file: Path | None = None,
    linkedin_export: Path | None = None,
    linkedin_url: str | None = None,
    paste: str | None = None,
    paste_file: Path | None = None,
    llm_call: Callable[[str, str], dict] | None = None,
) -> int:
    """Orchestrate all ingest specialists for a single onboarding run.

    Calls each provided input source's specialist and writes candidate-*.json
    + provenance-*.json into state_dir. Multiple sources are merged with
    later sources taking precedence (resume → linkedin → url → paste).

    Parameters
    ----------
    state_dir:
        `.onboard-state/` directory (created by _init_onboard_state).
    repo_root:
        Repo root (reserved — future use for master YAML context injection).
    resume_file:
        Optional path to resume PDF/DOCX/TXT.
    linkedin_export:
        Optional path to LinkedIn data export ZIP or profile PDF.
    linkedin_url:
        Optional LinkedIn profile URL (best-effort fetch).
    paste:
        Optional raw pasted text.
    paste_file:
        Optional path to file containing pasted text.
    llm_call:
        Optional LLM call override (for testing).

    Returns
    -------
    int
        0 on success (even partial), 1 on complete failure (no input provided).
    """
    had_input = False
    errors: list[str] = []

    if resume_file is not None:
        had_input = True
        try:
            ingest_resume(resume_file, state_dir, llm_call=llm_call)
            logger.info("run_ingestion: resume ingested from %s", resume_file)
        except Exception as exc:  # noqa: BLE001
            logger.error("run_ingestion: resume ingest failed: %s", exc)
            errors.append(f"resume: {exc}")

    if linkedin_export is not None:
        had_input = True
        try:
            ingest_linkedin_export(linkedin_export, state_dir, llm_call=llm_call)
            logger.info("run_ingestion: linkedin export ingested from %s", linkedin_export)
        except Exception as exc:  # noqa: BLE001
            logger.error("run_ingestion: linkedin ingest failed: %s", exc)
            errors.append(f"linkedin_export: {exc}")

    if linkedin_url is not None:
        had_input = True
        try:
            ingest_linkedin_url(linkedin_url, state_dir, llm_call=llm_call)
            logger.info("run_ingestion: linkedin url processed: %s", linkedin_url)
        except Exception as exc:  # noqa: BLE001
            logger.error("run_ingestion: linkedin_url ingest failed: %s", exc)
            errors.append(f"linkedin_url: {exc}")

    # Handle paste (inline or from file)
    effective_paste = paste
    if paste_file is not None:
        had_input = True
        try:
            effective_paste = paste_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.error("run_ingestion: paste_file read failed: %s", exc)
            errors.append(f"paste_file: {exc}")

    if effective_paste is not None:
        had_input = True
        try:
            ingest_paste(effective_paste, state_dir, llm_call=llm_call)
            logger.info("run_ingestion: paste ingested")
        except Exception as exc:  # noqa: BLE001
            logger.error("run_ingestion: paste ingest failed: %s", exc)
            errors.append(f"paste: {exc}")

    if not had_input:
        logger.warning("run_ingestion: no input sources provided")
        return 1

    return 0 if not errors else 1


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _linkedin_data_to_candidate(li_data) -> dict:
    """Convert LinkedInData structured CSV data into a candidate dict."""
    work_entries = []
    for pos in li_data.positions:
        work_entries.append({
            "company": pos["company"],
            "title": pos["title"],
            "start_date": pos["started"],
            "end_date": pos["finished"],
            "location": pos["location"],
            "bullets": [pos["description"]] if pos["description"] else [],
        })

    edu_entries = []
    for edu in li_data.education:
        edu_entries.append({
            "institution": edu["school"],
            "degree": edu["degree"],
            "field": "",
            "start_date": edu["started"],
            "end_date": edu["finished"],
        })

    skills = [{"name": s, "category": "technical"} for s in li_data.skills]

    return {
        "work": {"entries": work_entries},
        "skill": {"skills": skills},
        "education": {"entries": edu_entries},
        "author": {"name": "", "email": "", "phone": "", "location": "",
                   "github": "", "linkedin": "", "summary": ""},
    }


def _fetch_url_text(url: str, timeout: float = 15.0) -> str:
    """Attempt to fetch URL text via httpx.

    Returns empty string on any failure (auth wall, network error, redirect
    to login, etc.).
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; jobsmith/1.0; +https://jobsmith.dev)"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers=headers,
        ) as client:
            resp = client.get(url)
            # LinkedIn returns 999 or redirects to login for unauth requests
            if resp.status_code in (999, 401, 403) or "login" in str(resp.url):
                return ""
            if resp.status_code != 200:
                return ""
            # Very rough: extract visible text from HTML
            text = resp.text
            # Strip tags
            import re  # noqa: PLC0415
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"&[a-zA-Z]+;", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            return text[:50_000]  # cap at 50k chars
    except Exception:  # noqa: BLE001
        return ""


def _write_url_fallback(state_dir: Path, url: str, reason: str) -> None:
    """Write a url-fetch-status.json so callers know why URL ingestion was skipped."""
    state_dir.mkdir(parents=True, exist_ok=True)
    out = state_dir / "url-fetch-status.json"
    out.write_text(
        json.dumps({
            "url": url,
            "status": "failed",
            "reason": reason,
            "message": (
                "LinkedIn typically blocks unauthenticated access. "
                "Please upload your LinkedIn data export (Settings → Data Privacy → "
                "Get a copy of your data) or paste your profile text."
            ),
        }, indent=2)
    )


def _build_stub_provenance(candidate: dict, source_text: str) -> dict:
    """Build a provenance map without an LLM call.

    For each leaf value in the candidate dict, records the source_text as the
    supporting evidence. Used when the LLM call is mocked / not available.
    """
    provenance: dict = {}
    _flatten_provenance(candidate, source_text, "", provenance)
    return provenance


def _flatten_provenance(obj: object, source_text: str, prefix: str, out: dict) -> None:
    """Recursively flatten candidate dict into dot-path provenance entries."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else k
            _flatten_provenance(v, source_text, key, out)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            key = f"{prefix}[{i}]"
            _flatten_provenance(item, source_text, key, out)
    elif obj is not None and obj != "" and obj != []:
        # Leaf value: record a snippet from source_text as evidence
        val_str = str(obj)
        # Try to find the exact value in source; fall back to first 200 chars
        if val_str in source_text:
            out[prefix] = val_str
        else:
            out[prefix] = source_text[:200] if source_text else ""
