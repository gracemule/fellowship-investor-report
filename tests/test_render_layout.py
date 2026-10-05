"""Layout properties that must hold however many sections the data supports."""

from __future__ import annotations

from pathlib import Path

from chui_reporter.render.notes import write_review_notes
from chui_reporter.render.report_writer import build_document


def _secs(*keys):
    return [{"key": k, "title": f"Title {k}", "present": True, "body": "In Q2 2026, the Fund grew."}
            for k in keys]


def test_omitting_a_section_leaves_no_numbering_gap(tmp_path):
    """The agent removed the macro section; the contents then read Section 2 -> Section 4."""
    entries = build_document(_secs("1.1", "1.2", "2.1", "4.1", "4.2", "5.1"), {}, {}, {},
                             tmp_path / "t.docx")
    labels = [e[1] for e in entries]
    majors = [l for l in labels if l.startswith("Section")]
    assert [m.split(" — ")[0] for m in majors] == ["Section 1", "Section 2", "Section 3", "Section 4"]
    assert "3.1 Title 4.1" in labels and "3.2 Title 4.2" in labels and "4.1 Title 5.1" in labels


def test_minor_numbers_are_consecutive_when_one_is_missing(tmp_path):
    entries = build_document(_secs("5.1", "5.3", "5.4"), {}, {}, {}, tmp_path / "t.docx")
    assert [e[1] for e in entries if e[0] == 2] == ["1.1 Title 5.1", "1.2 Title 5.3", "1.3 Title 5.4"]


def test_a_section_with_nothing_in_it_is_not_rendered(tmp_path):
    secs = _secs("1.1") + [{"key": "2.1", "title": "Empty", "present": True, "body": ""}]
    entries = build_document(secs, {}, {}, {}, tmp_path / "t.docx")
    assert all("Empty" not in e[1] for e in entries)


class _Store:
    def __init__(self, notes):
        self._n = notes

    def review_notes(self):
        return self._n


def test_review_notes_are_written_apart_from_the_report(tmp_path):
    notes = [{"severity": "info", "area": "B", "text": "fyi"},
             {"severity": "decision", "area": "A", "text": "pick a basis"}]
    p = write_review_notes(_Store(notes), tmp_path / "n.md")
    text = Path(p).read_text()
    assert text.index("Decisions needed") < text.index("For information")
    assert "Not part of the report" in text and "pick a basis" in text
