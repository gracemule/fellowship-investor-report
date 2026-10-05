"""Reader for previously-published report PDFs.

These are needed for two reasons: they supply the prior-period comparative
columns, and they are the only record of what was actually told to LPs -- which
is what a reconciliation has to be checked against.

pypdf's layout extraction mode preserves column alignment well enough to parse
the wide Portfolio Performance Summary; the default mode collapses each cell
onto its own line and loses the row association.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

from .companies import resolve

# "1  Acme  FinTech  Kenya  Apr-2023  SAFE  2.4%  400  400  1,000  24  0.1 x  0.0%  2028  Yes"
_ROW = re.compile(
    r"^\s*(?P<idx>\d{1,2})\s+(?P<rest>\S.*?)\s*$"
)
_NUM = re.compile(r"-?[\d,]+(?:\.\d+)?")


@dataclass
class PublishedPosition:
    index: int
    company: str
    cost_usd: float
    fair_value_100_usd: float
    fair_value_chui_usd: float
    multiple: float | None
    page: int

    @property
    def implied_multiple(self) -> float | None:
        if not self.cost_usd:
            return None
        return self.fair_value_chui_usd / self.cost_usd


def _nums(line: str) -> list[float]:
    return [float(m.group(0).replace(",", "")) for m in _NUM.finditer(line)]


def read_portfolio_performance(pdf_path: str | Path) -> list[PublishedPosition]:
    """Parse section 5.4 from a published quarterly report.

    Returns one row per portfolio company, with the figures as published --
    i.e. what the LPs were actually given, whatever the source workbooks say.
    """
    reader = PdfReader(str(pdf_path))
    for page_no, page in enumerate(reader.pages, start=1):
        text = page.extract_text(extraction_mode="layout")
        if "Portfolio Performance Summary" not in text:
            continue
        out: list[PublishedPosition] = []
        for line in text.splitlines():
            m = _ROW.match(line)
            if not m:
                continue
            rest = m.group("rest")
            canon = _company_in(rest)
            if canon is None:
                continue
            values = _nums(rest)
            # Trailing numeric run: %FD, cost, unrealized cost, FV100, FVChui,
            # multiple, IRR, exit year. Percentages carry a '%' suffix which the
            # number regex strips, so positions are taken from the right.
            tail = [v for v in values]
            if len(tail) < 7:
                continue
            # ... cost, unrealized_cost, fv100, fv_chui, multiple, irr, year
            year_i = next(
                (i for i in range(len(tail) - 1, -1, -1) if 2020 <= tail[i] <= 2040), None
            )
            if year_i is None or year_i < 5:
                continue
            multiple = tail[year_i - 2]
            fv_chui = tail[year_i - 3]
            fv_100 = tail[year_i - 4]
            cost = tail[year_i - 6]
            out.append(
                PublishedPosition(
                    index=int(m.group("idx")),
                    company=canon,
                    cost_usd=cost * 1_000,
                    fair_value_100_usd=fv_100 * 1_000,
                    fair_value_chui_usd=fv_chui * 1_000,
                    multiple=multiple,
                    page=page_no,
                )
            )
        if out:
            return out
    raise ValueError(f"no Portfolio Performance Summary found in {pdf_path}")


def _company_in(text: str) -> str | None:
    """Longest company name appearing at the start of the row text."""
    head = text[:30]
    best = None
    for n in range(len(head), 2, -1):
        canon = resolve(head[:n].strip(), strict=False)
        if canon:
            best = canon
            break
    return best
