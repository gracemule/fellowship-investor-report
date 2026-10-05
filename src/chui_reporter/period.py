"""The reporting period, as a first-class setting.

The first version of this system had "Q2 2026" in 80-odd places: labels, file names,
sheet names, page numbers. That is a prototype, not a product -- the next quarter would
have broken it. Everything now asks the period for its wording, and the period is set
per workspace (default from CHUI_PERIOD, e.g. "2026Q2").
"""

from __future__ import annotations

import calendar
import os
import re
from dataclasses import dataclass
from datetime import date

_MONTH = {1: "January", 2: "February", 3: "March", 4: "April", 5: "May", 6: "June",
          7: "July", 8: "August", 9: "September", 10: "October", 11: "November", 12: "December"}


@dataclass(frozen=True, order=True)
class Period:
    year: int
    q: int

    def __post_init__(self) -> None:
        if self.q not in (1, 2, 3, 4):
            raise ValueError(f"quarter must be 1-4, got {self.q}")

    # ---- identity -----------------------------------------------------------
    @classmethod
    def parse(cls, text: str) -> Period:
        """Accepts '2026Q2', 'Q2 2026', 'Q2-2026', '2026-Q2'."""
        s = text.strip().upper().replace("-", " ").replace("_", " ")
        m = re.fullmatch(r"(20\d{2})\s*Q([1-4])", s) or re.fullmatch(r"Q([1-4])\s*(20\d{2})", s)
        if not m:
            raise ValueError(f"cannot read a reporting period from {text!r} (try '2026Q2')")
        a, b = m.groups()
        return cls(int(a), int(b)) if len(a) == 4 else cls(int(b), int(a))

    @property
    def label(self) -> str:                    # "Q2 2026"
        return f"Q{self.q} {self.year}"

    @property
    def code(self) -> str:                     # "2026Q2"
        return f"{self.year}Q{self.q}"

    @property
    def prev(self) -> Period:
        return Period(self.year - 1, 4) if self.q == 1 else Period(self.year, self.q - 1)

    @property
    def next(self) -> Period:
        return Period(self.year + 1, 1) if self.q == 4 else Period(self.year, self.q + 1)

    # ---- dates ----------------------------------------------------------------
    @property
    def start(self) -> date:
        return date(self.year, 3 * self.q - 2, 1)

    @property
    def end(self) -> date:
        m = 3 * self.q
        return date(self.year, m, calendar.monthrange(self.year, m)[1])

    @property
    def end_label(self) -> str:                # "30 June 2026"
        return f"{self.end.day} {_MONTH[self.end.month]} {self.year}"

    @property
    def end_short(self) -> str:                # "30 Jun 2026"
        return f"{self.end.day} {_MONTH[self.end.month][:3]} {self.year}"

    @property
    def end_iso(self) -> str:                  # "2026-06-30"
        return self.end.isoformat()

    @property
    def range_label(self) -> str:              # "April – June 2026"
        return f"{_MONTH[self.start.month]} – {_MONTH[self.end.month]} {self.year}"

    @property
    def year_start_label(self) -> str:         # "1 Jan"
        return "1 Jan"

    @property
    def is_first_quarter(self) -> bool:
        return self.q == 1

    def __str__(self) -> str:
        return self.label


def current() -> Period:
    """The period being reported. Set per run by the runner; default from the environment."""
    raw = os.environ.get("CHUI_PERIOD", "").strip()
    return Period.parse(raw) if raw else Period(2026, 2)


def set_current(p: Period) -> None:
    os.environ["CHUI_PERIOD"] = p.code
