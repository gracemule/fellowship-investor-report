"""Extractor for the per-company portfolio valuation workbooks.

These 19 workbooks share a layout but almost nothing else: a survey of the
corpus found **16 distinct sheet-name signatures across 19 files**. The same
quarter is variously called "Q2 June 2026", "Q2-2026 Val. Rep", "Q2 Jun 2026",
"Q2 2026" and "Q2-2026 Val Rep >>>". Sheet selection therefore goes through
`parse_quarter`, never through a literal name.

Five workbooks have no Q2 2026 sheet at all (their latest marks are Q1 2026,
Q4 2025 or -- for WiASSUR -- Q3 2025), yet the published Q2 report carries a
fair value for every company. Those marks were carried forward unchanged. That
is defensible, but it has to be *visible*, so every snapshot records the quarter
it actually came from and `is_stale_for` reports the gap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .workbook import ExtractionError, Provenance, Sheet, Value, Workbook, normalise_label

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


@dataclass(frozen=True, order=True)
class Quarter:
    year: int
    q: int

    def __str__(self) -> str:
        return f"Q{self.q} {self.year}"

    @property
    def index(self) -> int:
        return self.year * 4 + (self.q - 1)

    def quarters_before(self, other: Quarter) -> int:
        return other.index - self.index


def parse_quarter(sheet_name: str) -> Quarter | None:
    """Recognise a reporting quarter in any of the corpus' naming styles.

    Handles 'Q2 June 2026', 'Q2-2026 Val. Rep', 'Q2 Jun 2026', 'Q2 2026',
    'Q4 24 Val. Rep', 'April 2025', 'Q2-2026 Val Rep >>>'.
    """
    s = normalise_label(sheet_name)
    # Explicit quarter with a 4-digit year: "q2 june 2026", "q2-2026", "q2 2026"
    m = re.search(r"\bq\s*([1-4])\b\D{0,12}?(20\d{2})", s)
    if m:
        return Quarter(int(m.group(2)), int(m.group(1)))
    # Explicit quarter with a 2-digit year: "q4 24 val. rep"
    m = re.search(r"\bq\s*([1-4])\b[\s\-]*(\d{2})\b", s)
    if m:
        return Quarter(2000 + int(m.group(2)), int(m.group(1)))
    # Month-name form: "april 2025"
    m = re.search(r"\b([a-z]{3})[a-z]*\s+(20\d{2})", s)
    if m and m.group(1) in MONTHS:
        return Quarter(int(m.group(2)), (MONTHS[m.group(1)] - 1) // 3 + 1)
    return None


@dataclass
class ValuationSnapshot:
    """One company's mark, with the quarter it actually came from."""

    company_name: str          # from the filename -- the reliable identifier
    sheet_company_name: str | None   # B3 display name, corroboration only
    sheet_legal_name: str | None     # C5 'Legal Name'
    identity_conflict: str | None    # set when the sheet disagrees with the file
    unit_conflict: str | None        # set when the declared unit is not the real one
    source_file: str
    sheet_name: str
    quarter: Quarter
    as_of: object | None
    equity_value: Value
    total_invested: Value
    fair_value: Value
    proceeds: Value
    gross_multiple: Value
    ownership_fd: Value
    methodology: str | None

    def is_stale_for(self, target: Quarter) -> bool:
        return self.quarter < target

    def staleness(self, target: Quarter) -> int:
        """How many quarters behind the reporting period this mark is."""
        return max(0, self.quarter.quarters_before(target))

    def check_fair_value_identity(self, tol: float = 5e-4) -> tuple[bool, str]:
        """fair_value should equal equity_value x ownership_fd.

        Reported rather than raised: a break means the sheet disagrees with
        itself, which is a finding for the console, not a crash.
        """
        if not (self.fair_value.is_usable and self.equity_value.is_usable
                and self.ownership_fd.is_usable):
            return False, "inputs unavailable"
        expected = float(self.equity_value.raw) * float(self.ownership_fd.raw)
        actual = float(self.fair_value.raw)
        if abs(expected - actual) <= tol:
            return True, ""
        return False, f"fair value {actual:.4f} != equity {self.equity_value.raw} x FD {self.ownership_fd.raw} = {expected:.4f}"


class ValuationWorkbook:
    """A single company's valuation workbook."""

    # The valuation block is labelled in column G; values run across H.. as a
    # series of valuation events, left to right, most recent last.
    BLOCK_LABELS = {
        "equity_value": "Equity Value",
        "total_invested": "Total invested",
        "fair_value": "Fund Fair Value",
        "proceeds": "Fund Proceeds",
        "gross_multiple": "Gross Multiple",
        "ownership_fd": "Ownership FD",
    }

    def __init__(self, path: str | Path) -> None:
        self.wb = Workbook.open(path)
        self.path = self.wb.path

    # Sheets that sit alongside the valuation sheet for the same quarter but
    # hold comparables, multiples or raw financials rather than the mark.
    NON_VALUATION_HINTS = (
        "comp", "multiple", "archive", "actual", "financial", "p&l", "pnl",
        "balance", " bs", "data", "breakdown",
    )

    @property
    def quarters(self) -> dict[Quarter, list[str]]:
        """Every quarter-shaped sheet, as candidates ranked best-first.

        A quarter can map to several sheets -- LAMI's Q2 2026 is both
        'Q2-2026 Val Rep >>>' and 'Q2-2026 Valuation Multiples'. Candidates
        whose names look like comps/financials are ranked last, and
        `sheet_for` additionally verifies the sheet really has a mark.
        """
        out: dict[Quarter, list[str]] = {}
        for name in self.wb.sheet_names:
            q = parse_quarter(name)
            if q is not None:
                out.setdefault(q, []).append(name)
        for q, names in out.items():
            names.sort(key=lambda n: (self._looks_non_valuation(n), self.wb.sheet_names.index(n)))
        return out

    @classmethod
    def _looks_non_valuation(cls, sheet_name: str) -> bool:
        s = normalise_label(sheet_name)
        return any(h in s for h in cls.NON_VALUATION_HINTS)

    def _has_valuation_block(self, sheet_name: str) -> bool:
        sh = self.wb.sheet(sheet_name)
        return bool(sh.find_prefix("Fund Fair Value")) and bool(sh.find_prefix("Ownership FD"))

    def latest_quarter(self) -> Quarter | None:
        qs = self.quarters
        return max(qs) if qs else None

    def sheet_for(self, target: Quarter, *, allow_carry_forward: bool = True) -> tuple[Quarter, str]:
        """Resolve the sheet to use for `target`.

        Exact quarter preferred; otherwise the most recent earlier quarter -- a
        carry-forward, which the caller sees via the returned Quarter. Within a
        quarter, only sheets that actually carry a valuation block qualify.
        """
        qs = self.quarters
        if not qs:
            raise ExtractionError(
                f"no quarter-shaped sheets in {self.path.name}: {self.wb.sheet_names}"
            )
        candidates = [target] if target in qs else []
        if allow_carry_forward:
            candidates += sorted((q for q in qs if q < target), reverse=True)
        for q in candidates:
            for name in qs[q]:
                if self._has_valuation_block(name):
                    return q, name
        raise ExtractionError(
            f"{self.path.name} has no sheet with a valuation block at or before {target} "
            f"(saw {self.wb.sheet_names})"
        )

    # Header cells that sit inside the valuation block but are not marks.
    NON_PERIOD_HEADERS = ("total", "%change", "change", "multiple", "exit value")

    @classmethod
    def _valuation_columns(cls, sheet: Sheet, header_row: int, start_col: int,
                           max_col: int = 30) -> list[int]:
        """Columns that represent a valuation event.

        Driven by the header row, never by whether a figure happens to be
        present: Agrilogiq's sheet carries a trailing column of zeros with no
        header, and treating that as the current mark reported a $0 fair value
        for a live position.

        Two header dialects exist in the corpus -- real dates ("2026-06-30") and
        text quarter labels ("Q2 2026") -- and some sheets append 'Total' and
        '%Change' columns that must not be mistaken for periods.
        """
        from datetime import date as _date, datetime as _dt

        cols: list[int] = []
        for c in range(start_col, max_col + 1):
            h = sheet.raw(header_row, c)
            if isinstance(h, (_dt, _date)):
                cols.append(c)
            elif isinstance(h, str) and h.strip():
                label = normalise_label(h)
                if any(bad in label for bad in cls.NON_PERIOD_HEADERS):
                    continue
                if parse_quarter(h) is not None:
                    cols.append(c)
        return cols

    def snapshot(self, target: Quarter) -> ValuationSnapshot:
        quarter, sheet_name = self.sheet_for(target)
        sh = self.wb.sheet(sheet_name)

        label_col = None
        rows: dict[str, int] = {}
        for key, label in self.BLOCK_LABELS.items():
            hits = sh.find_prefix(label)
            if not hits:
                continue
            r, c = hits[0]
            rows[key] = r
            label_col = c if label_col is None else label_col

        if "fair_value" not in rows or label_col is None:
            raise ExtractionError(
                f"{self.path.name}::{sheet_name} has no recognisable valuation block"
            )

        # The 'In US$ Million' row carries one date per valuation event; the
        # rightmost dated column is the current mark. Every line is read from
        # that same column so the figures are internally consistent.
        header_hits = sh.find_prefix("In US$ Million", column=label_col)
        if not header_hits:
            raise ExtractionError(
                f"{self.path.name}::{sheet_name} has no 'In US$ Million' header row"
            )
        header_row = header_hits[0][0]
        cols = self._valuation_columns(sh, header_row, label_col + 1)
        if not cols:
            raise ExtractionError(
                f"{self.path.name}::{sheet_name} has no dated valuation column "
                f"(header row {header_row})"
            )
        col = cols[-1]

        def read(key: str, unit: str) -> Value:
            if key not in rows:
                # Line absent from this workbook's block -- record it as
                # quarantined rather than inventing a zero.
                return Value(
                    raw=None,
                    unit=unit,
                    provenance=Provenance(
                        file_path=str(self.path),
                        file_sha256=self.wb.sha256,
                        sheet=sheet_name,
                        cell="-",
                        anchor_label=self.BLOCK_LABELS[key],
                    ),
                    status="quarantined",
                    note=f"{self.BLOCK_LABELS[key]!r} row not present in this workbook",
                )
            return sh.at(rows[key], col, unit=unit, anchor_label=self.BLOCK_LABELS[key],
                         anchor_cell=sh.addr(rows[key], label_col))

        # The block header always reads "In US$ Million", but several workbooks
        # populate it in absolute dollars (e.g. an equity value of 9,500,000). Taking
        # the label at face value would report a $9.5bn seed-stage company, so
        # the scale is detected from magnitude and the conflict recorded.
        probe = sh.raw(rows.get("total_invested", rows["fair_value"]), col)
        money_unit = "USD_millions"
        unit_conflict = None
        if isinstance(probe, (int, float)) and abs(probe) >= 1000:
            money_unit = "USD"
            unit_conflict = (
                f"header says 'In US$ Million' but values are absolute USD "
                f"(probe {probe:,.0f}); treated as USD"
            )

        display = _clean(sh.raw(3, 2))
        legal = _clean(sh.raw(5, 3))
        file_name = company_from_filename(self.path)
        conflict = _identity_conflict(file_name, display, legal)

        return ValuationSnapshot(
            company_name=file_name,
            sheet_company_name=display,
            sheet_legal_name=legal,
            identity_conflict=conflict,
            unit_conflict=unit_conflict,
            source_file=str(self.path),
            sheet_name=sheet_name,
            quarter=quarter,
            as_of=sh.raw(15, col),
            equity_value=read("equity_value", money_unit),
            total_invested=read("total_invested", money_unit),
            fair_value=read("fair_value", money_unit),
            proceeds=read("proceeds", money_unit),
            gross_multiple=read("gross_multiple", "ratio"),
            ownership_fd=read("ownership_fd", "ratio"),
            methodology=_first_str(sh, 3, range(21, 25)),
        )

    def close(self) -> None:
        self.wb.close()

    def __enter__(self) -> ValuationWorkbook:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _clean(v: object) -> str | None:
    return s if isinstance(v, str) and (s := v.strip()) else None


def company_from_filename(path: Path) -> str:
    """Canonical company name from the filename.

    The filename is the only identifier that survived the corpus intact. One
    workbook (WiASSUR) carries another company's name in its display cell, so
    sheet contents corroborate rather than decide.
    """
    stem = path.stem
    stem = re.sub(r"^\s*\d+\s*[.\-]?\s*", "", stem)              # leading "01. "
    stem = re.sub(
        r"[-\s]*(portfolio\s*)?valuation\s*report.*$", "", stem, flags=re.I
    )
    stem = re.sub(r"\bQ[1-4][\s-]*20\d{2}\b", "", stem, flags=re.I)
    return re.sub(r"\s+", " ", stem).strip(" -.")


def _identity_conflict(file_name: str, display: str | None, legal: str | None) -> str | None:
    """Flag a sheet whose own name cells do not corroborate the filename."""
    key = normalise_label(file_name).replace(" ", "")
    for candidate in (display, legal):
        if candidate:
            c = normalise_label(candidate).replace(" ", "")
            if c.startswith(key[:5]) or key.startswith(c[:5]):
                return None
    seen = " / ".join(x for x in (display, legal) if x) or "(blank)"
    return f"filename says {file_name!r} but sheet says {seen}"


def _first_str(sheet: Sheet, row: int, cols) -> str | None:
    for c in cols:
        v = sheet.raw(row, c)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def discover(directory: str | Path) -> list[Path]:
    """Valuation workbooks in `directory`, excluding Excel lock files."""
    return sorted(p for p in Path(directory).glob("*.xlsx") if not p.name.startswith("~$"))
