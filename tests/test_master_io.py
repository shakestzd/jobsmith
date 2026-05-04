"""Tests for src/jobsmith/master_io.py — comment-safe YAML round-trip.

TDD: these tests were written BEFORE the implementation.  Run them to confirm
they fail (ImportError / AssertionError) before src/jobsmith/master_io.py exists,
then implement until they all pass.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from ruamel.yaml import YAML

from jobsmith.master_io import MasterSection, load_master, save_benchmark, save_master

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


FIXTURE_WORK = Path(__file__).parent / "fixtures" / "master_work.yml"


def _rparse(text: str) -> Any:
    """Parse YAML text with ruamel (round-trip mode) and return the object."""
    y = YAML()
    y.preserve_quotes = True
    return y.load(text)


def _comment_lines(text: str) -> list[str]:
    """Return all lines that start with '#' (stripped)."""
    return [ln.strip() for ln in text.splitlines() if ln.strip().startswith("#")]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def work_yaml_path(tmp_path: Path) -> Path:
    """Copy the fixture work YAML into a tmp dir and return its path."""
    dest = tmp_path / "work.yml"
    shutil.copy(FIXTURE_WORK, dest)
    return dest


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLoadMaster:
    def test_load_master_preserves_comments(self, tmp_path: Path) -> None:
        """load_master() returns a CommentedMap / CommentedSeq that retains comments."""
        path = tmp_path / "work.yml"
        shutil.copy(FIXTURE_WORK, path)

        result = load_master(MasterSection.WORK, path)

        # ruamel CommentedSeq — verify it is a list-like with ≥2 entries
        assert len(result) >= 2  # type: ignore[arg-type]

        # Serialise back and check comment lines survived
        y = YAML()
        import io

        buf = io.StringIO()
        y.dump(result, buf)
        dumped = buf.getvalue()
        comments = _comment_lines(dumped)
        assert any("anchor" in c.lower() or "position" in c.lower() or "fixture" in c.lower() or "comment" in c.lower() for c in comments), (
            f"Expected at least one comment in round-tripped YAML, got: {comments!r}"
        )

    def test_load_master_returns_commented_seq_for_list_sections(self, tmp_path: Path) -> None:
        """work/skill/education sections load as CommentedSeq (list-like)."""
        from ruamel.yaml.comments import CommentedSeq

        path = tmp_path / "work.yml"
        shutil.copy(FIXTURE_WORK, path)
        result = load_master(MasterSection.WORK, path)
        assert isinstance(result, CommentedSeq)

    def test_load_master_missing_file_raises(self, tmp_path: Path) -> None:
        """load_master raises FileNotFoundError when file does not exist."""
        missing = tmp_path / "nonexistent.yml"
        with pytest.raises(FileNotFoundError):
            load_master(MasterSection.WORK, missing)


class TestSaveMaster:
    def test_save_master_preserves_unchanged_keys_and_comments(
        self, work_yaml_path: Path
    ) -> None:
        """Updating one key leaves comments and other keys intact."""
        original_text = work_yaml_path.read_text(encoding="utf-8")
        original_comments = _comment_lines(original_text)
        assert original_comments, "Fixture must contain comment lines"

        # Payload: change title of first entry only
        payload = [
            {
                "title": "UPDATED TITLE",
                "location": "Acme Corp",
                "date": "Jan 2023 - Present",
                "description": "Remote",
                "details": [
                    "Unlocked $250M in additional Investment Tax Credits across 200K+ assets",
                    "Shipped 7 automated ETL pipelines at 99.9% reliability",
                ],
            },
            {
                "title": "Data Engineer",
                "location": "Previous Corp",
                "date": "Jun 2020 - Dec 2022",
                "description": "Hybrid",
                "details": [
                    "Built an optimizer that allocated 788 MW of capacity ($4.25B FMV)",
                    "Reduced processing time by 75%",
                ],
            },
        ]

        save_master(MasterSection.WORK, payload, work_yaml_path)

        new_text = work_yaml_path.read_text(encoding="utf-8")
        new_comments = _comment_lines(new_text)

        # Title was updated
        assert "UPDATED TITLE" in new_text

        # Second entry's title is unchanged
        assert "Data Engineer" in new_text

        # At least some comments preserved
        assert new_comments, f"No comments in output:\n{new_text}"
        # The inline detail comment should survive
        assert any("anchor" in c.lower() or "250M" in c or "comment" in c.lower() for c in new_comments), (
            f"Expected anchor/detail comment to survive. Comments found: {new_comments!r}\n\nFull text:\n{new_text}"
        )

    def test_save_master_validates_payload_raises_on_bad_schema(
        self, work_yaml_path: Path
    ) -> None:
        """Payload missing required 'title' field raises ValidationError, no write."""
        original_mtime = work_yaml_path.stat().st_mtime

        bad_payload = [{"location": "Acme Corp"}]  # missing required 'title'

        with pytest.raises(ValidationError):
            save_master(MasterSection.WORK, bad_payload, work_yaml_path)

        # File must NOT have been modified
        assert work_yaml_path.stat().st_mtime == original_mtime, (
            "save_master wrote the file even though validation failed"
        )

    def test_save_master_atomic_no_partial_write(
        self, work_yaml_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the write fails mid-way, the original file is untouched."""
        original_text = work_yaml_path.read_text(encoding="utf-8")

        # Simulate a failure during the rename step
        def failing_replace(self: Path, target: Path) -> None:
            raise OSError("simulated rename failure")

        monkeypatch.setattr(Path, "replace", failing_replace)

        payload = [
            {
                "title": "Should Not Appear",
                "location": "Acme Corp",
                "date": "Jan 2023 - Present",
                "description": "Remote",
                "details": [],
            },
        ]

        with pytest.raises(OSError, match="simulated rename failure"):
            save_master(MasterSection.WORK, payload, work_yaml_path)

        # Original file content must be unchanged
        assert work_yaml_path.read_text(encoding="utf-8") == original_text
        # No tmp file should remain
        tmp_files = list(work_yaml_path.parent.glob(".work.yml.*.tmp"))
        assert not tmp_files, f"Orphaned tmp file(s) found: {tmp_files}"

    def test_save_master_key_order_preserved(self, work_yaml_path: Path) -> None:
        """Key insertion order in each entry is preserved after a round-trip."""
        payload = [
            {
                "title": "Senior Data Engineer",
                "location": "Acme Corp",
                "date": "Jan 2023 - Present",
                "description": "Remote",
                "details": [
                    "Unlocked $250M in additional Investment Tax Credits across 200K+ assets",
                    "Shipped 7 automated ETL pipelines at 99.9% reliability",
                ],
            },
            {
                "title": "Data Engineer",
                "location": "Previous Corp",
                "date": "Jun 2020 - Dec 2022",
                "description": "Hybrid",
                "details": [
                    "Built an optimizer that allocated 788 MW of capacity ($4.25B FMV)",
                    "Reduced processing time by 75%",
                ],
            },
        ]
        save_master(MasterSection.WORK, payload, work_yaml_path)

        text = work_yaml_path.read_text(encoding="utf-8")
        # 'title' must appear before 'location' in the file
        title_pos = text.index("title:")
        location_pos = text.index("location:")
        assert title_pos < location_pos, "Key order not preserved: 'title' should precede 'location'"


class TestSaveBenchmark:
    def test_save_benchmark_writes_content(self, tmp_path: Path) -> None:
        """save_benchmark writes the text verbatim."""
        path = tmp_path / "benchmark.md"
        content = "# Benchmark\n\nSome content here.\n"
        save_benchmark(content, path)
        assert path.read_text(encoding="utf-8") == content

    def test_save_benchmark_atomic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """save_benchmark does not leave a partial file on rename failure."""
        def failing_replace(self: Path, target: Path) -> None:
            raise OSError("simulated rename failure")

        monkeypatch.setattr(Path, "replace", failing_replace)

        path = tmp_path / "benchmark.md"
        with pytest.raises(OSError, match="simulated rename failure"):
            save_benchmark("# content", path)

        # File should not exist (was never written)
        assert not path.exists()
        # No tmp files should linger
        tmp_files = list(tmp_path.glob(".benchmark.md.*.tmp"))
        assert not tmp_files

    def test_save_benchmark_creates_parent_dirs(self, tmp_path: Path) -> None:
        """save_benchmark creates missing parent directories."""
        path = tmp_path / "nested" / "dir" / "benchmark.md"
        save_benchmark("# hello", path)
        assert path.read_text(encoding="utf-8") == "# hello"


class TestMasterSection:
    def test_section_enum_has_four_values(self) -> None:
        values = {s.value for s in MasterSection}
        assert values == {"work", "skill", "education", "author"}
