"""jobsmith.onboard.parsers.extract — text extraction from PDF, DOCX, and ZIP.

No fabrication: extraction is purely mechanical — no LLM calls here.
The LLM structuring step is in ingest.py.
"""
from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path
from typing import NamedTuple


class LinkedInData(NamedTuple):
    """Parsed content from a LinkedIn export ZIP or profile PDF."""

    positions: list[dict]  # list of {company, title, description, started, finished}
    education: list[dict]  # list of {school, degree, started, finished}
    skills: list[str]      # flat list of skill names
    raw_text: str          # joined text for LLM fallback


def extract_pdf_text(path: Path) -> str:
    """Extract text from a PDF using pdfplumber.

    Returns empty string if pdfplumber cannot read the file (rather than
    raising), so callers can fall back to direct LLM reading.
    """
    try:
        import pdfplumber  # noqa: PLC0415
    except ImportError:
        return ""

    try:
        with pdfplumber.open(str(path)) as pdf:
            pages = []
            for page in pdf.pages:
                text = page.extract_text() or ""
                if text.strip():
                    pages.append(text)
            return "\n\n".join(pages)
    except Exception:  # noqa: BLE001
        return ""


def extract_docx_text(path: Path) -> str:
    """Extract text from a DOCX file using python-docx.

    Returns empty string on failure so callers can fall back.
    """
    try:
        from docx import Document  # noqa: PLC0415
    except ImportError:
        return ""

    try:
        doc = Document(str(path))
        lines = []
        for para in doc.paragraphs:
            text = para.text.strip()
            if text:
                lines.append(text)
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return ""


def _parse_csv_rows(content: str) -> list[dict]:
    """Parse a CSV string into a list of dicts."""
    reader = csv.DictReader(io.StringIO(content))
    return [dict(row) for row in reader]


def extract_linkedin_zip(path: Path) -> LinkedInData:
    """Parse a LinkedIn 'Download your data' ZIP.

    Looks for Positions.csv, Education.csv, and Skills.csv.
    Falls back gracefully when a file is missing.
    """
    positions: list[dict] = []
    education: list[dict] = []
    skills: list[str] = []

    try:
        with zipfile.ZipFile(str(path), "r") as zf:
            names_lower = {n.lower(): n for n in zf.namelist()}

            if "positions.csv" in names_lower:
                raw = zf.read(names_lower["positions.csv"]).decode("utf-8", errors="replace")
                rows = _parse_csv_rows(raw)
                for row in rows:
                    positions.append({
                        "company": row.get("Company Name", "").strip(),
                        "title": row.get("Title", "").strip(),
                        "description": row.get("Description", "").strip(),
                        "location": row.get("Location", "").strip(),
                        "started": row.get("Started On", "").strip(),
                        "finished": row.get("Finished On", "").strip(),
                    })

            if "education.csv" in names_lower:
                raw = zf.read(names_lower["education.csv"]).decode("utf-8", errors="replace")
                rows = _parse_csv_rows(raw)
                for row in rows:
                    education.append({
                        "school": row.get("School Name", "").strip(),
                        "degree": row.get("Degree Name", "").strip(),
                        "notes": row.get("Notes", "").strip(),
                        "started": row.get("Date Attended", "").strip(),
                        "finished": row.get("Date Finished", "").strip(),
                    })

            if "skills.csv" in names_lower:
                raw = zf.read(names_lower["skills.csv"]).decode("utf-8", errors="replace")
                rows = _parse_csv_rows(raw)
                for row in rows:
                    name = row.get("Name", "").strip()
                    if name:
                        skills.append(name)

    except (zipfile.BadZipFile, OSError):
        pass

    # Build a human-readable raw text for LLM fallback / provenance
    raw_parts: list[str] = []
    if positions:
        raw_parts.append("=== Work Experience ===")
        for p in positions:
            raw_parts.append(
                f"{p['title']} at {p['company']} ({p['started']} - {p['finished']}): {p['description']}"
            )
    if education:
        raw_parts.append("=== Education ===")
        for e in education:
            raw_parts.append(
                f"{e['degree']} from {e['school']} ({e['started']} - {e['finished']})"
            )
    if skills:
        raw_parts.append("=== Skills ===")
        raw_parts.append(", ".join(skills))

    return LinkedInData(
        positions=positions,
        education=education,
        skills=skills,
        raw_text="\n".join(raw_parts),
    )
