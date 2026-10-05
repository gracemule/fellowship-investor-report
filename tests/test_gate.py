"""The numeric gate. Each test pins a flaw found in the first version, which
flagged dates, exempted fabricated short figures, used a loose tolerance, and
never looked at tables."""

from __future__ import annotations

import pytest

from chui_reporter.render.gate import check_grounded, gate_report

LEDGER = {8_326_000.0, 12_400_000.0, 614_545.45, 0.6851, 1.18, -84_213.0,
          2_450_000.0, 512_000.0, 4_218_640.55}


def flagged(text, allowed=LEDGER):
    return check_grounded(text, allowed)


# -- what must pass ----------------------------------------------------------


@pytest.mark.parametrize("text", [
    "The portfolio was valued at $8.3M.",              # rounds from 8,326,000
    "The portfolio was valued at $8.33M.",
    "Total committed equity capital is $12,400,000.",
    "Contributed capital was $4,218,640.55.",
    "Alpha Co's mark was $615K.",                       # 614,545.45 -> 615K
    "About 68.51% of the commitment was called.",       # ledger holds 0.6851
    "Alpha Co carried a 1.18x unrealized multiple.",
    "The vehicle recorded a loss of $(84,213).",       # sign is not compared
    "Revenue reached $512K.",
])
def test_supported_figures_pass(text):
    assert flagged(text) == []


@pytest.mark.parametrize("text", [
    "The second call was received on 21 May 2026, per the bank statement.",
    "As at 31 March 2026, the comparative column is shown.",
    "In April 2026, the fund closed. In 2026, the fund grew.",
    "Capital calls 3 and 4 completed on 12 April 2026 and 21 May 2026.",
    "The 12 companies span 4 markets; Q2 2026 followed Q1 2026.",
])
def test_dates_counts_and_years_are_not_figures(text):
    """Regression: '21 May' was read as 21 million; '2026,' kept its comma."""
    assert flagged(text) == []


# -- what must be caught -----------------------------------------------------


@pytest.mark.parametrize("text,token", [
    ("The portfolio was valued at $3.8M.", "$3.8M"),     # transposed digits
    ("The portfolio was valued at $8.4M.", "$8.4M"),     # 0.9% off: a loose tolerance passes this
    ("The first vehicle's commitment was $6.8M.", "$6.8M"),
    ("Total invested rose 12% on the quarter.", "12%"),  # short, but not bare
    ("We deployed $7M in the quarter.", "$7M"),          # short, but not bare
    ("A 5x multiple on cost.", "5x"),
    ("Net IRR was 5.5%.", "5.5%"),
    ("The LP commitment was $2.4M.", "$2.4M"),           # 2,450,000 is $2.5M at 1dp
    ("Fair value was $8,326,001.", "$8,326,001"),        # off by one dollar
])
def test_unsupported_figures_are_caught(text, token):
    """The short ones are the hole in the first version, which exempted any
    one- or two-digit number even with a $, % or unit attached."""
    assert token in flagged(text)


def test_rounding_is_judged_at_the_precision_the_text_shows():
    allowed = {8_326_000.0}
    assert flagged("$8.3M", allowed) == [] and flagged("$8.33M", allowed) == []
    assert "$8.4M" in flagged("$8.4M", allowed)
    assert flagged("$8M", allowed) == [], "8.326M rounds to 8M at 0dp"


# -- tables ------------------------------------------------------------------


def _table(rows):
    return {"t_fv": {"columns": ["Company", "Fair value"], "rows": rows}}


def test_table_cells_are_checked_not_just_prose():
    off = gate_report([], _table([["Alpha Co", "614,545.45"], ["Beta Co", "617,000"]]), LEDGER)
    assert len(off) == 1 and "Beta Co" in off[0] or "617,000" in off[0]
    assert "table t_fv row 2 [Fair value]" in off[0], "the message must say where"


def test_a_clean_table_passes():
    assert gate_report([], _table([["Alpha Co", "614,545.45"], ["Fund", "$8.3M"]]), LEDGER) == []


def test_numeric_cell_types_are_checked_too():
    assert gate_report([], _table([["Alpha Co", 614545.45], ["X", 617000]]), LEDGER) != []


def test_gate_reports_prose_and_tables_together():
    sections = [{"key": "1.4", "body": "Valued at $3.8M."}]
    off = gate_report(sections, _table([["X", "617,000"]]), LEDGER)
    assert any(o.startswith("section 1.4") for o in off) and any("table t_fv" in o for o in off)
