"""What the agent needs, and what each source unlocks.

The user keeps working the way they always have: documents go in a local folder. The
agent has to be able to say, at any moment, exactly what it has and what it is waiting
for -- not as a file listing, but as *readiness*: "Macroeconomic snapshot: waiting for a
file in Macro and Context". That is what a slot is. Files are matched by folder and name
pattern, never by exact name, so a re-issued workpaper is still recognised.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass


@dataclass(frozen=True)
class Slot:
    id: str
    label: str                    # what the user calls it
    folder: str                   # folder under the workspace root (case-insensitive)
    patterns: tuple[str, ...]     # file name globs (case-insensitive)
    required: bool
    unlocks: tuple[str, ...]      # report sections that depend on it (original keys)
    hint: str                     # what to put there, in plain words
    minimum: int = 1
    durable: bool = False         # stands from quarter to quarter (the brand kit): uploaded once, never asked for again


SLOTS: tuple[Slot, ...] = (
    Slot("delaware", "Delaware fund financial package", "Fund Financials",
         ("*financial package*.pdf", "*fund i, lp*.pdf"), True, ("2.1", "2.2", "4.1", "4.2"),
         "The quarter's Chui Ventures Fund I, LP financial statements (PDF)."),
    Slot("lp_workpaper", "Mauritius LP workpapers", "Fund Financials",
         ("*chui ventures lp*.xlsx", "*lp*.xlsx"), True, ("2.1", "2.2", "4.1", "4.2"),
         "The Chui Ventures LP working papers (Excel) for the period."),
    Slot("gp_workpaper", "Management company workpapers", "Fund Financials", ("*chui ventures gp*.xlsx", "*gp*.xlsx"), False, (),
         "The Chui Ventures GP (management company) working papers (Excel), if you have them."),
    Slot("fund_model", "Fund model", "Portfolio Company Data", ("*fund model*.xlsx",), True,
         ("2.1", "2.2", "4.1", "4.2", "5.1", "5.2", "5.3", "5.4"),
         "The fund model workbook with Portfolio Valuation and Summary Report sheets."),
    Slot("portfolio_metrics", "Portfolio metrics", "Portfolio Company Data",
         ("*portfolio metrics*.xlsx",), True, ("5.5", "1.3"),
         "The Portfolio Metrics workbook with a Key Metrics sheet for the quarter."),
    Slot("valuations", "Company valuation reports", "Valuation Reports", ("*.xlsx",), True,
         ("1.3", "1.4", "5.4"), "One valuation workbook per portfolio company.", minimum=1),
    Slot("prior_report", "Previous quarter's report", "Prior Period Baseline", ("*report*.pdf",),
         True, ("4.1", "4.2", "1.4"), "The published report for the previous quarter (PDF), for comparatives."),
    Slot("pipeline", "Subsequent events and pipeline", "Pipeline and subsequent events",
         ("*.pdf", "*.docx"), False, ("1.5",), "Facility drawdowns, approvals, signed letters."),
    Slot("brand_logos", "Brand logos", "Branding", ("cv_logo_*.png", "cv_icon_*.png"), True, (),
         "The Chui Ventures logo and icon PNGs (the Branding folder).", durable=True),
    Slot("brand_fonts", "Brand fonts", "Branding", ("larken*.ttf",), True, (),
         "The Larken font files the report is typeset in (Branding/Fonts/Larken).", minimum=3, durable=True),
    Slot("macro", "Macroeconomic data", "Macro and Context", ("*",), False, ("3.1",),
         "Country indicators (GDP, inflation, policy rates, FX) for the markets you invest in."),
    Slot("gp_statement", "General Partner statement", "GP statement", ("*",), False, ("1.5",),
         "The GP's commentary on governance matters and fund-level developments."),
)

BY_ID = {s.id: s for s in SLOTS}
DURABLE_FOLDERS = frozenset(s.folder.casefold() for s in SLOTS if s.durable)


def is_durable(path: str) -> bool:
    """Does this file belong to the brand kit and the like, which carry from quarter to quarter?"""
    parts = path.replace("\\", "/").strip("/").split("/")
    return len(parts) > 1 and parts[0].casefold() in DURABLE_FOLDERS


def _norm(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def slot_files(slot: Slot, paths: list[str]) -> list[str]:
    """The workspace paths that satisfy `slot`."""
    out = []
    for raw in paths:
        path = _norm(raw)
        parts = path.split("/")
        if len(parts) < 2:
            continue
        if parts[0].casefold() != slot.folder.casefold():
            continue
        name = parts[-1].casefold()
        if name.startswith(("~$", ".")):
            continue
        if any(fnmatch.fnmatch(name, pat.casefold()) for pat in slot.patterns):
            out.append(path)
    return sorted(out)


def slot_for_path(path: str) -> Slot | None:
    """Which slot a single file belongs to, preferring the more specific (non-wildcard)."""
    hits = [s for s in SLOTS if slot_files(s, [path])]
    if not hits:
        return None
    return sorted(hits, key=lambda s: (s.patterns == ("*",), len(s.patterns)))[0]
