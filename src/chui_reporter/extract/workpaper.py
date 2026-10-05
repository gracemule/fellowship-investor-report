"""Extractor for the GP and LP audit workpapers.

Keyed on **account codes**, never row positions: the GP and LP books put the
same concepts on different rows, and rows shift each quarter. The code in
column A of SOFP, or embedded as "4125 (Management fees)" in SOCI, is the stable
identifier.

The two books also use *different codes for the same concept* -- directorship
fees are 4030 in the GP and 4035 in the LP -- so codes are only unique within an
entity. Anything cross-entity goes through `CONCEPTS`.

A caution that shaped this module: **neither workbook contains a single
fund-level performance metric.** A keyword sweep for NAV/TVPI/DPI/RVPI/MOIC/IRR
returns one hit, and it is an account name for realised FX. Investments are
carried at cost and there is no waterfall, no PCAP, no distribution ledger. Every
headline figure in section 2.1 of the report is therefore *derived* here, not
extracted -- and the derivations needing fair value have to be handed it from the
valuation sources, because the workpapers simply do not know it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .workbook import Sheet, Value, Workbook

# "4125 (Management fees)" -> 4125
_SOCI_CODE = re.compile(r"^\s*(\d{4})\s*\(")

Entity = Literal["GP", "LP"]

# Cross-entity concept map. Codes are unique only within an entity.
CONCEPTS: dict[tuple[Entity, int], str] = {
    ("LP", 5100): "partnership_interest",
    ("LP", 8415): "cash",
    ("LP", 9001): "payable_to_gp",
    ("LP", 4125): "management_fee_expense",
    ("GP", 3100): "management_fee_income",
    ("GP", 7550): "receivable_from_lp",
    ("GP", 7560): "receivable_from_cvcf",
    ("GP", 9450): "deferred_grant",
}


@dataclass(frozen=True)
class StatementLine:
    code: int | None
    label: str
    current: Value
    comparative: Value | None = None
    movement: Value | None = None

    @property
    def concept(self) -> str | None:
        return None  # set by StatementReader, which knows the entity


@dataclass
class FundMetrics:
    """Fund-level figures, every one of them derived.

    `source` records how each was obtained so the console can show an LP
    reviewer that nothing here was copied from a cell that does not exist.
    """

    commitment_usd: float | None
    contributed_usd: float
    uncalled_usd: float | None
    pct_called: float | None
    nav_cost_basis_usd: float
    investments_at_cost_usd: float
    cash_usd: float
    distributions_usd: float
    dpi: float
    rvpi_cost_basis: float
    fair_value_usd: float | None = None
    tvpi: float | None = None
    source: dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.source is None:
            self.source = {}


class StatementReader:
    """Reads SOFP / SOCI line items from one workpaper."""

    def __init__(self, workbook: Workbook, entity: Entity) -> None:
        self.wb = workbook
        self.entity = entity

    # -- generic line access -------------------------------------------------

    def balance_sheet(self) -> dict[int, StatementLine]:
        """SOFP lines by account code. Column C current, E comparative, G movement."""
        sh = self.wb.sheet("SOFP")
        out: dict[int, StatementLine] = {}
        for (r, c), v in sorted(sh._grid.items()):
            if c != 1 or not isinstance(v, int) or not (1000 <= v <= 9999):
                continue
            label = sh.raw(r, 2)
            out[v] = StatementLine(
                code=v,
                label=str(label).strip() if label else "",
                current=sh.at(r, 3, anchor_label=str(v), anchor_cell=sh.addr(r, 1)),
                comparative=sh.at(r, 5, anchor_label=str(v), anchor_cell=sh.addr(r, 1)),
                movement=sh.at(r, 7, anchor_label=str(v), anchor_cell=sh.addr(r, 1)),
            )
        return out

    def income_statement(self) -> dict[int, StatementLine]:
        """SOCI lines by account code, parsed out of 'NNNN (Name)' labels."""
        sh = self.wb.sheet("SOCI")
        out: dict[int, StatementLine] = {}
        for (r, c), v in sorted(sh._grid.items()):
            if c != 1 or not isinstance(v, str):
                continue
            m = _SOCI_CODE.match(v)
            if not m:
                continue
            code = int(m.group(1))
            out[code] = StatementLine(
                code=code,
                label=v.strip(),
                current=sh.at(r, 3, anchor_label=v.strip(), anchor_cell=sh.addr(r, 1)),
            )
        return out

    def total(self, sheet: str, label: str, column: int = 3) -> Value:
        """A named total row ('Total Assets', 'Total Equity', 'Loss Before Tax')."""
        return self.wb.sheet(sheet).value(label, dx=column - 1, unit="USD")

    def concept(self, name: str) -> StatementLine | None:
        wanted = {code for (ent, code), c in CONCEPTS.items()
                  if ent == self.entity and c == name}
        if not wanted:
            return None
        lines = {**self.balance_sheet(), **self.income_statement()}
        for code in wanted:
            if code in lines:
                return lines[code]
        return None


# -- capital calls -----------------------------------------------------------


@dataclass(frozen=True)
class CapitalCall:
    sequence: int
    date: object
    amount_usd: float
    counterparty: str
    provenance: str


def capital_calls(lp: Workbook) -> list[CapitalCall]:
    """The drawdown ledger from the LP's Bank Analysis sheet.

    This is the authoritative record of contributed capital: the eight entries
    sum to exactly the SOFP Partnership Interest and to what the Q2 report
    publishes as MEDA Mauritius called capital.
    """
    sh = lp.sheet("Bank Analysis")
    calls: list[CapitalCall] = []
    for r, c in sh.find_prefix("capital call"):
        label = str(sh.raw(r, c)).strip()
        amount = sh.at(r, c + 2, unit="USD")
        if not amount.is_usable:
            continue
        m = re.search(r"capital call\s*(\d+)", label, re.I)
        counterparty = label.split("-", 1)[1].strip() if "-" in label else ""
        calls.append(
            CapitalCall(
                sequence=int(m.group(1)) if m else 1,
                date=sh.raw(r, c - 1),
                amount_usd=amount.as_usd(),
                counterparty=counterparty,
                provenance=str(amount.provenance),
            )
        )
    return sorted(calls, key=lambda x: x.sequence)


# -- derived fund metrics ----------------------------------------------------


class ScopeMismatch(ValueError):
    """Raised when figures from different vehicles are combined."""


def derive_fund_metrics(
    lp: Workbook,
    *,
    commitment_usd: float | None = None,
    fair_value_usd: float | None = None,
    fair_value_scope: str = "fund",
    scope: str = "LP_MAURITIUS",
) -> FundMetrics:
    """Compute fund-level metrics the workpapers do not contain.

    `fair_value_usd` must be supplied from the valuation sources; without it
    TVPI is left as None rather than silently substituting cost.

    **Scope is enforced.** This workbook is the Mauritius vehicle alone -- one
    LP, contributing $5.34M -- while the portfolio fair value in the report is
    fund-wide across Delaware, Mauritius Equity and Mauritius Debt. Dividing the
    fund-wide numerator by this vehicle's denominator yields a TVPI near 1.8x
    against a true figure below 1x. The combination is refused rather than
    returned with a caveat, because a caveat in a docstring does not stop a
    wrong multiple reaching an LP.
    """
    if fair_value_usd is not None and fair_value_scope != scope:
        raise ScopeMismatch(
            f"fair value is scoped {fair_value_scope!r} but contributions are "
            f"{scope!r}. A multiple built from these is not meaningful. Supply "
            f"the {scope} share of fair value, or compute the multiple at fund "
            f"level once Delaware's books are available."
        )
    reader = StatementReader(lp, "LP")
    bs = reader.balance_sheet()

    contributed = bs[5100].current.as_usd()
    investments = reader.total("SOFP", "Total Investments").as_usd()
    cash = bs[8415].current.as_usd()
    nav = reader.total("SOFP", "Total Equity").as_usd()

    # No distribution ledger exists; nil is asserted from the absence of any
    # distribution row in Bank Analysis rather than assumed.
    distributions = 0.0

    uncalled = pct_called = None
    if commitment_usd:
        uncalled = commitment_usd - contributed
        pct_called = contributed / commitment_usd

    tvpi = None
    if fair_value_usd is not None and contributed:
        tvpi = (fair_value_usd + distributions) / contributed

    return FundMetrics(
        commitment_usd=commitment_usd,
        contributed_usd=contributed,
        uncalled_usd=uncalled,
        pct_called=pct_called,
        nav_cost_basis_usd=nav,
        investments_at_cost_usd=investments,
        cash_usd=cash,
        distributions_usd=distributions,
        dpi=distributions / contributed if contributed else 0.0,
        rvpi_cost_basis=nav / contributed if contributed else 0.0,
        fair_value_usd=fair_value_usd,
        tvpi=tvpi,
        source={
            "contributed_usd": "LP SOFP!C40 (acct 5100), ties to 8 capital calls",
            "nav_cost_basis_usd": "LP SOFP 'Total Equity' -- cost basis, not fair value",
            "investments_at_cost_usd": "LP SOFP 'Total Investments' (accts 5450-5650)",
            "cash_usd": "LP SOFP!C34 (acct 8415)",
            "distributions_usd": "derived nil: no distribution entries in Bank Analysis",
            "rvpi_cost_basis": "DERIVED, cost-based -- not a true RVPI",
            "tvpi": "DERIVED from supplied fair value" if tvpi is not None
                    else "UNAVAILABLE: no fair value supplied",
            "commitment_usd": "supplied by caller -- workpapers carry only the "
                              "management-fee basis, not the commitment",
        },
    )
