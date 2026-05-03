"""Tests for jobsmith.marimo.directive_parser — AMEND grammar parser.

Covers:
- Simple replace directive
- Indexed section + indexed field
- Append (op=append) directive
- Multiple directives in mixed text
- UUID4 identity (not stable hash)
- Invalid section name rejection
"""
from __future__ import annotations

import uuid

from jobsmith.marimo.directive_parser import Amendment, parse_amendments

# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


def test_parse_simple_directive():
    """AMEND cover-letter.opening: shorten → 1 amendment, section=cover-letter, op=replace."""
    text = "AMEND cover-letter.opening: shorten"
    amendments = parse_amendments(text)
    assert len(amendments) == 1
    a = amendments[0]
    assert a.section == "cover-letter"
    assert a.field == "opening"
    assert a.op == "replace"
    assert a.value == "shorten"
    assert a.index is None
    assert a.status == "pending"


def test_parse_indexed_directive():
    """AMEND work[0].bullet[2]: quantify → section=work, index=0, field=bullet[2]."""
    text = "AMEND work[0].bullet[2]: quantify"
    amendments = parse_amendments(text)
    assert len(amendments) == 1
    a = amendments[0]
    assert a.section == "work"
    assert a.index == 0
    assert a.field == "bullet[2]"
    assert a.op == "replace"
    assert a.value == "quantify"


def test_parse_append_directive():
    """AMEND skills.technical[+]: add "Polars" → op=append."""
    text = 'AMEND skills.technical[+]: add "Polars"'
    amendments = parse_amendments(text)
    assert len(amendments) == 1
    a = amendments[0]
    assert a.section == "skills"
    assert a.field == "technical"
    assert a.op == "append"
    assert a.value == 'add "Polars"'


def test_parse_multiple_directives():
    """Multiline text with prose + 3 directives → exactly 3 amendments; prose ignored."""
    text = (
        "Here is my review of your resume. I have a few suggestions:\n"
        "\n"
        "AMEND cover-letter.opening: emphasize cross-functional impact\n"
        "AMEND work[0].bullet[2]: tighten and quantify\n"
        'AMEND skills.technical[+]: add "Polars"\n'
        "\n"
        "Let me know if you have questions.\n"
    )
    amendments = parse_amendments(text)
    assert len(amendments) == 3
    sections = {a.section for a in amendments}
    assert sections == {"cover-letter", "work", "skills"}


def test_amendment_ids_are_uuid4():
    """Parse the same directive twice → different UUID4s; both valid uuid4."""
    text = "AMEND cover-letter.opening: shorten"
    a1 = parse_amendments(text)[0]
    a2 = parse_amendments(text)[0]
    # IDs must be distinct (not stable hash)
    assert a1.id != a2.id
    # Both must be valid UUID4
    parsed1 = uuid.UUID(a1.id, version=4)
    parsed2 = uuid.UUID(a2.id, version=4)
    assert parsed1.version == 4
    assert parsed2.version == 4


def test_invalid_section_rejected():
    """AMEND foo.bar: ... where foo is not in VALID_SECTIONS → empty list."""
    text = "AMEND foo.bar: do something"
    amendments = parse_amendments(text)
    assert amendments == []


def test_invalid_section_mixed_with_valid():
    """Invalid section directives are skipped; valid ones still parsed."""
    text = (
        "AMEND bogus.opening: ignore this\n"
        "AMEND work.summary: keep this\n"
    )
    amendments = parse_amendments(text)
    assert len(amendments) == 1
    assert amendments[0].section == "work"


def test_all_valid_sections_accepted():
    """All five valid section names are accepted."""
    directives = [
        "AMEND work.summary: test",
        "AMEND education.degree: test",
        "AMEND skills.technical: test",
        "AMEND cover-letter.opening: test",
        "AMEND fit-score.pitch: test",
    ]
    for directive in directives:
        amendments = parse_amendments(directive)
        assert len(amendments) == 1, f"Expected 1 amendment for: {directive}"


def test_amendment_dataclass_fields():
    """Amendment dataclass has all expected fields."""
    a = Amendment(
        id=str(uuid.uuid4()),
        section="work",
        index=0,
        field="bullet[1]",
        op="replace",
        value="some text",
    )
    assert a.status == "pending"
    assert a.section == "work"
    assert a.op == "replace"


def test_parse_empty_text():
    """Empty string → empty list (no crash)."""
    assert parse_amendments("") == []


def test_parse_prose_only():
    """Text with no AMEND directives → empty list."""
    text = "This is a regular response with no directives.\nAll good!"
    assert parse_amendments(text) == []
