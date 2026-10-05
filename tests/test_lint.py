"""The report must never narrate its own data problems. Each 'must reject' line
is taken from the first end-to-end run, which shipped them as the final report."""

from __future__ import annotations

import pytest

from chui_reporter.render.lint import lint_report, lint_text

FROM_THE_FIRST_RUN = [
    "This section cannot be completed from the source documents.",
    "The Macro and Context folder is empty and no macro data is available.",
    "The Fund's own workpapers carry no fund-level performance metrics.",
    "We report both rather than selecting one.",
    "The per-company valuation workbooks total $4,812,305.20; the two sources differ by $603,118.40.",
    "The figures we present in this report are derived, and we say so at each point of use.",
    "Delaware's books are not in the source set for this report.",
    "TVPI is marked unavailable because no fair value is supplied.",
    "The schedule is reconstructed from the bank analysis entries.",
    "Here is the report for your review.",
    "Contributed and uncalled capital: Not available.",
    "Data to be provided by the fund administrator.",
    "I could not verify the figure against the ledger.",
]


@pytest.mark.parametrize("text", FROM_THE_FIRST_RUN)
def test_meta_commentary_from_the_first_run_is_rejected(text):
    assert lint_text(text, "t"), f"should have been rejected: {text!r}"


@pytest.mark.parametrize("text", [
    "In Q2 2026, Chui Ventures deployed $450K across two follow-on investments.",
    "Beta Co is held at cost pending its next priced round.",
    "Investments are carried at fair value, with unrealized gains of $360K.",
    "The Fund made no distributions during the quarter.",
    "Revenue grew 106% year on year to $850K, and the company remains near break-even.",
    "Nigeria's inflation held at 15.9%, and the policy rate was unchanged at 26.5%.",
    "Following Investment Committee approval, the facility will be drawn in Q3 2026.",
    "The Fund's net asset value was $11.9M at 30 June 2026.",
    "Management fees are calculated on committed capital.",
])
def test_ordinary_report_prose_is_not_flagged(text):
    assert lint_text(text, "t") == [], f"false positive: {text!r}"


def test_tables_titles_and_cover_are_linted_too():
    tables = {"t": {"title": "Fund Summary (derived from workpapers)", "columns": ["Metric", "Value"],
                    "rows": [["TVPI", "Not available"], ["NAV", "$11.9M"]]}}
    out = lint_report([], tables, meta={"subtitle": "Draft for review"})
    wheres = {v.where for v in out}
    assert "table t title" in wheres and "table t row 1" in wheres and "cover subtitle" in wheres


def test_violations_say_where_and_why():
    v = lint_text("Intro. The data is missing for this quarter.", "section 3.1")[0]
    assert v.where == "section 3.1" and v.why and "missing" in v.context
