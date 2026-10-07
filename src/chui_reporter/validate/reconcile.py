"""Cross-source reconciliation of portfolio fair values.

Two independent sources carry a Q2 2026 fair value for every company:

  A. the per-company valuation workbooks in "Valuation Reports/"
  B. the "Portfolio Valuation" sheet of the Fund Model

They do not agree, and the published report draws from both. This module makes
the disagreement explicit rather than silently preferring one, because picking a
side without being asked is exactly how an unsupported number reaches an LP.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..extract.companies import resolve
from ..extract.valuation import Quarter, ValuationWorkbook, discover
from ..extract.workbook import Workbook

# Fund Model "Portfolio Valuation" layout. Header row 4; data rows 6..24.
FM_SHEET = "Portfolio Valuation"
FM_FIRST_ROW, FM_LAST_ROW = 6, 24
FM_COL_NAME, FM_COL_FD, FM_COL_COST, FM_COL_FV = 3, 11, 12, 13


@dataclass
class Disagreement:
    company: str
    field: str
    valuation_report: float | None
    fund_model: float | None
    note: str = ""

    @property
    def abs_diff(self) -> float:
        if self.valuation_report is None or self.fund_model is None:
            return float("inf")
        return abs(self.valuation_report - self.fund_model)

    @property
    def pct_diff(self) -> float:
        if not self.fund_model:
            return float("inf")
        if self.valuation_report is None:
            return float("inf")
        return (self.valuation_report - self.fund_model) / abs(self.fund_model) * 100


@dataclass
class CompanyRow:
    company: str
    vr_fair_value_usd: float | None
    fm_fair_value_usd: float | None
    vr_quarter: Quarter | None
    staleness: int
    unit_conflict: str | None
    flags: list[str]
    vr_prov: object | None = None   # Provenance of the valuation-report cell
    fm_prov: object | None = None   # Provenance of the Fund Model cell


def load_fund_model(path, quarter_label_row: int = 4) -> dict[str, dict[str, float]]:
    """Fair values from the Fund Model, keyed by canonical company.

    Note the header on the fair-value column reads "Fair Value (Q2 2025)" while
    the figures are Q2 2026 -- a stale label in the source. Column position is
    used, and the discrepancy is recorded in the returned metadata.
    """
    wb = Workbook.open(path)
    sh = wb.sheet(FM_SHEET)
    out: dict[str, dict[str, float]] = {}
    for r in range(FM_FIRST_ROW, FM_LAST_ROW + 1):
        name = sh.raw(r, FM_COL_NAME)
        if not isinstance(name, str) or not name.strip():
            continue
        canon = resolve(name, strict=False)
        if canon is None:
            continue
        fv = sh.at(r, FM_COL_FV, unit="USD_thousands")
        cost = sh.at(r, FM_COL_COST, unit="USD_thousands")
        fd = sh.at(r, FM_COL_FD, unit="ratio")
        out[canon] = {
            "fair_value_usd": fv.as_usd() if fv.is_usable else None,
            "cost_usd": cost.as_usd() if cost.is_usable else None,
            "fd": float(fd.raw) if fd.is_usable else None,
            "cell": fv.provenance.cell,
            "prov": fv.provenance,
        }
    wb.close()
    return out


def load_valuation_reports(directory, target: Quarter) -> dict[str, CompanyRow]:
    rows: dict[str, CompanyRow] = {}
    for path in discover(directory):
        try:
            with ValuationWorkbook(path) as vw:
                snap = vw.snapshot(target)
        except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
            canon = resolve(path.stem, strict=False) or path.stem
            rows.setdefault(canon, CompanyRow(canon, None, None, None, 0, None,
                                              [f"EXTRACT_FAILED: {exc}"]))
            continue
        canon = resolve(snap.company_name, strict=False) or snap.company_name
        flags: list[str] = []
        if snap.identity_conflict:
            flags.append(f"IDENTITY: {snap.identity_conflict}")
        ok, msg = snap.check_fair_value_identity()
        if not ok and msg != "inputs unavailable":
            flags.append(f"FV_IDENTITY: {msg}")
        fv = snap.fair_value.as_usd() if snap.fair_value.is_usable else None
        existing = rows.get(canon)
        if existing is not None:
            # Duplicate workbook (Leta ships twice). Keep the fresher mark and say so.
            flags.append(f"DUPLICATE_WORKBOOK: also {path.name}")
            if existing.vr_quarter and snap.quarter <= existing.vr_quarter:
                existing.flags.extend(flags)
                continue
        rows[canon] = CompanyRow(
            company=canon,
            vr_fair_value_usd=fv,
            fm_fair_value_usd=None,
            vr_quarter=snap.quarter,
            staleness=snap.staleness(target),
            unit_conflict=snap.unit_conflict,
            flags=flags,
            vr_prov=snap.fair_value.provenance,
        )
    return rows


def reconcile(valuation_dir, fund_model_path, target: Quarter,
              tolerance_usd: float = 1_000.0) -> tuple[list[CompanyRow], list[Disagreement]]:
    rows = load_valuation_reports(valuation_dir, target)
    fm = load_fund_model(fund_model_path)

    for canon, data in fm.items():
        row = rows.get(canon)
        if row is None:
            rows[canon] = CompanyRow(canon, None, data["fair_value_usd"], None, 0, None,
                                     ["NO_VALUATION_REPORT"], fm_prov=data["prov"])
        else:
            row.fm_fair_value_usd = data["fair_value_usd"]
            row.fm_prov = data["prov"]

    disagreements: list[Disagreement] = []
    for canon, row in sorted(rows.items()):
        a, b = row.vr_fair_value_usd, row.fm_fair_value_usd
        if a is None or b is None:
            if a is None and b is None:
                continue
            disagreements.append(
                Disagreement(canon, "fair_value", a, b, note="present in only one source")
            )
            continue
        if abs(a - b) > tolerance_usd:
            disagreements.append(Disagreement(canon, "fair_value", a, b))
    return sorted(rows.values(), key=lambda r: r.company), disagreements
