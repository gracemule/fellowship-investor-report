"""The numeric gate: no figure reaches a document unless the ledger supports it.

Membership is exact, not similar. A figure is accepted only if some ledger value,
rounded the way the *text itself* is rounded, equals it. That is stricter than a
percentage tolerance, which would let a fabricated $9.8M pass against a true
$9,741,000. And it fails closed: an unrecognised figure blocks the render.

Rules, each learned from a defect in the first version:

* The tokenizer takes a unit suffix only when it is attached to the number
  ($883K, $9.7M, 5x, 76.24%). Otherwise "26 May" reads as 26 million.
* Trailing punctuation is never part of a number ("2026," is the year 2026).
* Only *bare* integers are exempt -- a year, or a one/two-digit count or day.
  Anything carrying $, a unit, a % or a decimal point must be grounded. Exempting
  "any short number" let a fabricated "$9M" or "12%" through.
* Tables are checked as well as prose. Most of a financial report's numbers live
  in tables, which the first version never looked at.

Known limit: comparison ignores sign, since a loss is written "(125,047)",
"-$125,047" or "a loss of $125,047" and the ledger may hold either sign. A
sign-flip is therefore not caught here; the statement identities are the check
for that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

_NUMERAL = re.compile(
    r"""
    (?<![\w.])                         # not inside a word or another number
    (?P<open>\()?
    (?P<prefix>[~$-]*)                 # ~ and $ and a minus sign
    (?P<num>\d+(?:,\d{3})*(?:\.\d+)?)
    (?P<unit>%|bn|Tr|[xXkKMBT])?       # attached units only (no whitespace)
    (?![A-Za-z\d])                     # a unit letter must not start a word
    """,
    re.X,
)

_SCALE = {"": 1, "%": 1, "x": 1, "X": 1, "k": 1e3, "K": 1e3, "M": 1e6, "B": 1e9,
          "bn": 1e9, "T": 1e12, "Tr": 1e12}


@dataclass(frozen=True)
class Token:
    text: str
    shown: Decimal
    decimals: int
    unit: str
    currency: bool

    @property
    def is_bare_integer(self) -> bool:
        return not (self.currency or self.unit or self.decimals or "," in self.text)

    @property
    def exempt(self) -> bool:
        """Years, and one- or two-digit counts and days -- nothing else."""
        if not self.is_bare_integer:
            return False
        n = int(self.shown)
        return 1900 <= n <= 2099 or n <= 99


def tokens(text: str) -> list[Token]:
    out = []
    for m in _NUMERAL.finditer(text):
        num = m.group("num")
        decimals = len(num.split(".")[1]) if "." in num else 0
        out.append(Token(
            text=m.group(0).strip(),
            shown=Decimal(num.replace(",", "")),
            decimals=decimals,
            unit=m.group("unit") or "",
            currency="$" in m.group("prefix"),
        ))
    return out


def _round(value: float, scale: float, decimals: int) -> Decimal:
    q = Decimal(1).scaleb(-decimals)
    return Decimal(repr(abs(value) / scale)).quantize(q, rounding=ROUND_HALF_UP)


def supported(tok: Token, allowed: set[float]) -> bool:
    """True if some ledger value, rounded as the text rounds it, equals the text."""
    scale = _SCALE[tok.unit]
    for a in allowed:
        candidates = [a]
        if tok.unit == "%":
            candidates.append(a * 100)  # the ledger may hold the ratio, not the percent
        for c in candidates:
            if _round(c, scale, tok.decimals) == tok.shown:
                return True
    return False


def check_grounded(text: str, allowed: set[float]) -> list[str]:
    """The figures in `text` that no ledger value supports."""
    return [t.text for t in tokens(text) if not t.exempt and not supported(t, allowed)]


def gate_report(sections: list[dict], tables: dict[str, dict],
                allowed: set[float]) -> list[str]:
    """Every unsupported figure in the report, with where it is."""
    offences: list[str] = []
    for s in sections:
        bad = check_grounded(s["body"], allowed)
        if bad:
            offences.append(f"section {s['key']}: {bad}")
    for key, t in sorted(tables.items()):
        for r, row in enumerate(t["rows"], start=1):
            for c, cell in enumerate(row):
                if cell is None:
                    continue
                bad = check_grounded(str(cell), allowed)
                if bad:
                    col = t["columns"][c] if c < len(t["columns"]) else c
                    offences.append(f"table {key} row {r} [{col}]: {bad}")
    return offences
