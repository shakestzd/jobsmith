"""AMEND directive parser for the jobsmith review notebook.

Grammar (two forms)
-------------------
Replace form:
    AMEND <section>[<index>].<field>[<field_index>]: <content>

Append form:
    AMEND <section>[<index>].<field>[+]: <content>

Valid section names: work, education, skills, cover-letter, fit-score.

Directives with unknown section names are silently skipped.
Extended operations (MOVE, DELETE, macro forms) are out of scope;
callers should surface a "hand-edit YAML and re-render" hint.

Examples
--------
    AMEND work[0].bullet[2]: tighten and quantify
    AMEND cover-letter.opening: emphasize cross-functional impact
    AMEND skills.technical[+]: add "Polars"
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

VALID_SECTIONS: frozenset[str] = frozenset(
    {"work", "education", "skills", "cover-letter", "fit-score"}
)

# Matches both replace and append forms.
# Groups:
#   section  — section name (e.g. "work", "cover-letter")
#   index    — optional [<n>] on the section (e.g. "[0]")
#   field    — optional field name after the dot (e.g. "bullet", "opening")
#   field_idx — optional [<n>] or [+] appended to the field
#   value    — everything after ": " on the same line
_AMEND_RE = re.compile(
    r"AMEND\s+"
    r"(?P<section>[a-z][a-z\-]*)"         # section name: lowercase + hyphens
    r"(?P<index>\[\d+\])?"                 # optional section index e.g. [0]
    r"(?:\.(?P<field>[a-z][a-z\-]*"        # optional .fieldname
    r"(?P<field_idx>\[\d+\])?))?"          # optional field index e.g. [2]
    r"(?P<append>\[\+\])?"                 # optional append marker [+]
    r"\s*:\s*"
    r"(?P<value>.+?)$",                    # value: rest of line (non-greedy + EOL)
    re.MULTILINE,
)


@dataclass
class Amendment:
    """A single parsed AMEND directive.

    Attributes
    ----------
    id:
        UUID4 string — unique per parse call (never stable hash).
    section:
        One of VALID_SECTIONS.
    index:
        Integer position on the section (e.g. work[0] → 0), or None.
    field:
        Field name within the section (may include index suffix like "bullet[2]"),
        or None if the directive targets the section itself.
    op:
        "replace" for plain directives; "append" when [+] is present.
    value:
        The proposed new content (stripped).
    status:
        Lifecycle status — default "pending".
    """

    id: str
    section: str
    index: int | None
    field: str | None
    op: str
    value: str
    status: str = field(default="pending")


def parse_amendments(text: str) -> list[Amendment]:
    """Parse all AMEND directives from *text* and return them as a list.

    Directives with invalid section names are silently dropped.
    Each returned Amendment gets a fresh UUID4 id (not a hash of content).

    Parameters
    ----------
    text:
        Raw text (e.g. a Claude response chunk or full reply).

    Returns
    -------
    List of :class:`Amendment` objects in the order they appear in *text*.
    """
    out: list[Amendment] = []
    for m in _AMEND_RE.finditer(text):
        section = m.group("section")
        if section not in VALID_SECTIONS:
            continue

        # Section-level index: "[0]" → 0
        raw_index = m.group("index")
        index = int(raw_index[1:-1]) if raw_index else None

        # Field: the "field" group captures the full "bullet[2]" or "opening" string.
        # field_idx is a nested group inside field — do NOT re-append it.
        field_str: str | None = m.group("field")  # e.g. "bullet[2]", "opening", or None

        op = "append" if m.group("append") else "replace"
        value = m.group("value").strip()

        out.append(
            Amendment(
                id=str(uuid.uuid4()),
                section=section,
                index=index,
                field=field_str,
                op=op,
                value=value,
            )
        )
    return out


__all__ = ["Amendment", "VALID_SECTIONS", "parse_amendments"]
