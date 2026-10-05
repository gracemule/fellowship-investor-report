"""Deterministic report builders.

Each reads real sources, records every figure it uses in the fact ledger with its
provenance (or as a derived fact computed here), and writes the finished table or
chart into the report. The agent decides *which* builders to run and writes the
narrative around them; it never types a table's numbers.

Everything shown to a reader is grounded by construction: displayed figures are
either a source cell or a derived fact whose inputs are named. Sections are
created only for what the sources actually support; gaps become review notes,
never text in the report.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader

from .. import config
from .. import period as pr
from ..extract.companies import resolve
from ..extract.workbook import Value, Workbook
from ..extract.workpaper import StatementReader
from .ledger import derived, fact_from_value
from .store import Fact, Store

SECTION_TITLES = {
    "2.1": "Fund Overview & Key Details",
    "2.2": "LP Commitments & Capital Accounts",
    "4.1": "Unaudited Balance Sheet",
    "4.2": "Unaudited Statement of Operations",
    "5.1": "Portfolio Breakdown by Sector",
    "5.2": "Portfolio Breakdown by Region / Country",
    "5.3": "Portfolio Composition by Company",
    "5.4": "Portfolio Performance Summary",
    "5.5": "Jobs & Impact Metrics",
}


def hu(x: float, dp: int = 0) -> Decimal:
    """Round half up -- the way a finance reader (and the numeric gate) rounds.
    Python's own formatting rounds half to even and would print 9,508 for 9,508.50."""
    return Decimal(repr(abs(x))).quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP)


def n0(x: float) -> str:
    return f"{hu(x):,}"


def n2(x: float) -> str:
    return f"{hu(x, 2):,}"


def pct1(x: float) -> str:
    return f"{hu(x * 100, 1)}%"


def money(x: float, dp: int = 0) -> str:
    """Whole dollars, negatives in brackets, zero as an en dash."""
    q = hu(x, dp)
    if q == 0:
        return "–"
    s = f"{q:,}"
    return f"({s})" if x < 0 else s


@dataclass
class Rec:
    """Collects the facts a builder relies on, and their values by label."""

    facts: list[Fact] = field(default_factory=list)
    vals: dict[str, float] = field(default_factory=dict)

    def src(self, label: str, value: float, *, file: str, sheet: str | None, cell: str | None,
            unit: str = "USD", note: str | None = None, as_of: str | None = None) -> float:
        self.facts.append(Fact(label, float(value), unit=unit, source_file=file, source_sheet=sheet,
                               source_cell=cell, as_of=as_of, status="extracted", note=note))
        self.vals[label] = float(value)
        return float(value)

    def value(self, label: str, v: Value, *, scale: float = 1.0, unit: str = "USD",
              note: str | None = None, as_of: str | None = None) -> float:
        """From an extracted Value, keeping its source cell."""
        if not v.is_usable or not isinstance(v.raw, (int, float)):
            raise ValueError(f"{label}: source cell {v.provenance} is not a usable number")
        p = v.provenance
        return self.src(label, float(v.raw) * scale, file=Path(p.file_path).name, sheet=p.sheet,
                        cell=p.cell, unit=unit, note=note, as_of=as_of)

    def drv(self, label: str, value: float, inputs: list[str], formula: str,
            unit: str = "USD") -> float:
        self.facts.append(derived(label, value, inputs, formula, unit))
        self.vals[label] = float(value)
        return float(value)

    def save(self, store: Store) -> int:
        return store.add_facts(self.facts)


def _ensure_section(store: Store, key: str, body: str | None = None) -> None:
    """Create the section if absent, without clobbering narrative already written."""
    existing = {s["key"]: s for s in store.sections()}
    if key in existing:
        return
    major, minor = (key.split(".") + ["0"])[:2]
    store.ensure_report("Chui Ventures Fund I", pr.current().label)
    store.set_section(key, SECTION_TITLES[key], body or "", int(major) * 100 + int(minor))


# ---------------------------------------------------------------------------
# Delaware: Chui Ventures Fund I, LP financial package (PDF)
# ---------------------------------------------------------------------------

_AMT = r"\(?-?\$[\d,]+\.\d{2}\)?|-"
_LINE1 = re.compile(rf"^\s*(?P<label>[A-Za-z][^$]*?)\s{{2,}}(?P<val>{_AMT})\s*$")
_LINE3 = re.compile(rf"^\s*(?P<label>[A-Za-z][^$]*?)\s{{2,}}(?P<a>{_AMT})\s+(?P<b>{_AMT})\s+(?P<c>{_AMT})\s*$")
_SUBTOTAL = re.compile(rf"^\s*(?P<cost>\$[\d,]+\.\d{{2}})\s+(?P<fv>\$[\d,]+\.\d{{2}})\s+(?P<ug>{_AMT})\s*$")
_MONTH = re.compile(r"^\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},")
_STOP = {"pre-", "seed", "safe", "preferred", "stock", "angel", "total", "round"}


def _amt(s: str) -> float:
    s = s.strip()
    if s == "-":
        return 0.0
    neg = s.startswith("(") or s.startswith("-")
    v = float(re.sub(r"[^\d.]", "", s))
    return -v if neg else v


def _page_of(path: Path, *needles: str, after: int = 0) -> int:
    """1-based page whose text contains every needle (case-insensitive). Pages are found by
    what is on them, not by number: a report that gains a page must not shift every figure."""
    reader = PdfReader(str(path))
    for i, pg in enumerate(reader.pages, start=1):
        if i <= after:
            continue
        text = " ".join((pg.extract_text(extraction_mode="layout") or "").split()).casefold()
        if all(" ".join(n.split()).casefold() in text for n in needles):
            return i
    raise ValueError(f"{path.name}: no page contains {needles}")


def _pdf_lines(path: Path, page: int) -> list[str]:
    import unicodedata

    text = PdfReader(str(path)).pages[page - 1].extract_text(extraction_mode="layout") or ""
    text = unicodedata.normalize("NFKC", text)       # "ﬁ" -> "fi", curly quotes, non-breaking spaces
    return [re.sub(r"[ \t]{3,}", "   ", ln).rstrip() for ln in text.splitlines() if ln.strip()]


def parse_delaware(store: Store) -> tuple[Rec, dict[str, float]]:
    pdf = config.require(config.delaware_package())
    name = pdf.name
    rec = Rec()
    key: dict[str, float] = {}

    def line_facts(prefix: str, page: int, as_of: str | None = None) -> None:
        for ln in _pdf_lines(pdf, page):
            m = _LINE1.match(ln)
            if m and m["val"] != "-":
                lab = f"{prefix}: {m['label'].strip()}"
                if lab not in rec.vals:
                    rec.src(lab, _amt(m["val"]), file=name, sheet=f"page {page}", cell=None, as_of=as_of)

    P = pr.current()
    p_bs = _page_of(pdf, "Statement of Assets, Liabilities")
    p_inc = _page_of(pdf, "Income Statement", "Total Expenses")
    p_chg = _page_of(pdf, "Statement of Changes in Investors")
    p_sched = _page_of(pdf, "Schedule of Investments")
    line_facts("Delaware balance sheet", p_bs, P.end_iso)
    line_facts("Delaware income statement", p_inc)

    # Statement of changes in investors' capital: LP | GP | Total
    for ln in _pdf_lines(pdf, p_chg):
        m = _LINE3.match(ln)
        if m:
            lab = m["label"].strip()
            for col, who in (("a", "Limited Partners"), ("b", "General Partner"), ("c", "Total")):
                rec.src(f"Delaware capital changes: {lab} [{who}]", _amt(m[col]), file=name,
                        sheet=f"page {p_chg}", cell=None)

    # Schedule of investments: per-company subtotal (cost, fair value, unrealized)
    company = None
    lines = [ln for pg in range(p_sched, p_chg) for ln in _pdf_lines(pdf, pg)]
    for i, ln in enumerate(lines):
        s = ln.strip()
        if _SUBTOTAL.match(ln) and company:
            m = _SUBTOTAL.match(ln)
            rec.src(f"Delaware schedule: {company} cost", _amt(m["cost"]), file=name, sheet=f"pages {p_sched}-{p_chg - 1}", cell=None)
            rec.src(f"Delaware schedule: {company} fair value", _amt(m["fv"]), file=name, sheet=f"pages {p_sched}-{p_chg - 1}", cell=None)
            company = None
            continue
        nxt = next((x for x in lines[i + 1:i + 4] if x.strip()), "")
        if (re.fullmatch(r"[A-Za-z][A-Za-z0-9 .,&()'’-]{3,}", s) and "$" not in s
                and s.lower() not in _STOP and not s.lower().startswith(("total", "tax", "portfolio", "company", "date", "chui", "schedule", "as of"))
                and _MONTH.match(nxt)):
            company = resolve(s, strict=False) or s
    return rec, key


# ---------------------------------------------------------------------------
# Mauritius LP: quarter-specific expenses read from the general ledger
# ---------------------------------------------------------------------------

_GL_ACCT = re.compile(r"^(\d{4}) \((?P<name>.+)\)$")


def lp_quarter_expenses(lp: Workbook, tag: str,
                        notes: list[str] | None = None) -> dict[int, tuple[str, float, str]]:
    """Expense (4xxx) totals for transactions whose reference carries `tag`.
    A reference with the right quarter but a wrong year ("TR 04 Q2 2027" is in the
    source) is still counted, and reported via `notes`.
    Returns code -> (account name, amount, source cell list)."""
    gl = lp.sheet("GL")
    quarter = tag.split()[0]
    out: dict[int, tuple[str, float, list[str]]] = {}
    code = None
    for r in sorted({r for (r, _c) in gl._grid}):
        a = gl.raw(r, 1)
        if isinstance(a, str) and (m := _GL_ACCT.match(a.strip())):
            code = int(m.group(1))
            out.setdefault(code, (m.group("name"), 0.0, []))
            continue
        ref = gl.raw(r, 2)
        if code and 4000 <= code < 5000 and isinstance(ref, str) and re.search(
                rf"\b{quarter} 20\d\d\b", ref):
            if tag not in ref and notes is not None:
                notes.append(f"GL {gl.addr(r, 2)} is referenced '{ref}' but dated within {tag}; "
                             f"counted in {tag} (year in the reference looks mistyped).")
            deb, cre = gl.raw(r, 5) or 0, gl.raw(r, 6) or 0
            name, amt, cells = out[code]
            out[code] = (name, amt + float(deb) - float(cre), cells + [gl.addr(r, 5 if deb else 6)])
    return {c: (n, a, ",".join(cs)) for c, (n, a, cs) in out.items() if cs}


# ---------------------------------------------------------------------------
# prior-quarter comparatives, read from the published Q1 report
# ---------------------------------------------------------------------------

_NUM_TOK = r"\(?[\d,]+(?:\.\d+)?\)?|-"
_NUMS = re.compile(rf"^\s*(?P<label>[A-Za-z][A-Za-z &/,().'-]*?)\s{{2,}}(?P<nums>(?:{_NUM_TOK})(?:\s+(?:{_NUM_TOK}))+)\s*$")


def _num(tok: str) -> float:
    return 0.0 if tok == "-" else (-1 if tok.startswith("(") else 1) * float(re.sub(r"[^\d.]", "", tok))


def pick(rows: dict[str, list[float]], name: str) -> list[float]:
    """A statement line by name, tolerating a renamed suffix ("INVESTMENTS" vs
    "INVESTMENTS (FAIR VALUE OF PORTFOLIO)") -- labels drift between quarters."""
    key = name.upper()
    if key in rows:
        return rows[key]
    for k, v in rows.items():
        if k.startswith(key):
            return v
    raise KeyError(f"the prior report has no line '{name}'; it has: {sorted(rows)[:14]}")


def fact_like(rec: Rec, prefix: str) -> tuple[str, float]:
    """The recorded prior-report fact whose label starts with `prefix` (label, value)."""
    for k, v in rec.vals.items():
        if k.startswith(prefix):
            return k, v
    raise KeyError(f"no recorded fact starting {prefix!r}")


def prior_report_path() -> Path:
    return config.require(config.prior_report(pr.current().prev))


def prior_rows(kind: str, rec: Rec, prefix: str) -> dict[str, list[float]]:
    """The previous quarter's published statement, whatever its layout.

    kind 'balance'    -> the first number on a line is the position at that quarter end
    kind 'operations' -> the last two numbers are that quarter's figure and year to date
    (a Q1 report prints [QTD, YTD], a Q2 report [Q1, QTD, YTD]; the last two are stable)."""
    pdf = prior_report_path()
    page = _page_of(pdf, "Unaudited Balance Sheet", "TOTAL ASSETS") if kind == "balance" else \
        _page_of(pdf, "Statement of Operations", "Total Investment Expenses")
    out: dict[str, list[float]] = {}
    for ln in _pdf_lines(pdf, page):
        m = _NUMS.match(ln)
        if not m:
            continue
        toks = m["nums"].split()
        vals = [_num(t) for t in toks]
        lab = " ".join(m["label"].split())
        if kind == "operations":
            vals = vals[-2:] if len(vals) >= 2 else vals * 2        # [quarter, year to date]
            toks = toks[-2:] if len(toks) >= 2 else toks * 2
        out[lab.upper()] = vals
        if toks[0] != "-":
            rec.src(f"{prefix}: {lab}", vals[0], file=pdf.name, sheet=f"page {page}", cell=None)
        if kind == "operations" and toks[-1] != "-":
            rec.src(f"{prefix}: {lab} [YTD]", vals[-1], file=pdf.name, sheet=f"page {page}", cell=None)
    return out


def pv_layout(pv) -> tuple[list[int], int]:
    """Company rows and totals row of the Fund Model's Portfolio Valuation sheet, found by
    content. A new portfolio company adds a row; fixed row numbers would silently drop it."""
    hdr = next(r for r in range(1, 25) if str(pv.raw(r, 3) or "").strip().lower() == "company name")
    rows: list[int] = []
    blanks, started = 0, False
    for r in range(hdr + 1, hdr + 90):
        name, cost = pv.raw(r, 3), pv.raw(r, 12)
        if isinstance(name, str) and name.strip() and isinstance(cost, (int, float)) \
                and name.strip().lower() != "total":
            rows.append(r)
            started, blanks = True, 0
        elif started:
            if isinstance(cost, (int, float)):
                return rows, r                      # the totals row: a number with no company name
            blanks += 1
            if blanks > 4:
                break
    raise ValueError("Portfolio Valuation: could not find the totals row under the company list")


def summary_layout(sr) -> dict[str, int]:
    """Row numbers of the commitments blocks on the Fund Model's Summary Report sheet."""
    col = lambda r: str(sr.raw(r, 3) or "").strip()  # noqa: E731
    rows = range(1, 90)
    de = next(r for r in rows if col(r).startswith("Chui Ventures Fund I LP"))
    mu = next(r for r in rows if col(r).startswith("Chui Ventures LP - Mauritius") and "debt" not in col(r).lower())
    debt = next(r for r in rows if col(r).startswith("Chui Ventures LP - Mauritius") and "debt" in col(r).lower())
    return {"hnwi": de + 2, "fo": de + 3, "msdf": de + 4, "meda_eq": de + 5, "de_total": de + 6,
            "mu": mu + 2, "equity": mu + 6, "debt": debt + 2, "grand": debt + 5}


def _label_row(sh, prefix: str, column: int = 2) -> int:
    hits = sh.find_prefix(prefix, column=column)
    if not hits:
        raise ValueError(f"{sh.name}: no row labelled {prefix!r}")
    return hits[0][0]


def metrics_sheet(pm: Workbook):
    """The 'Key Metrics' sheet for the reporting quarter, however the tab is spelled."""
    from ..extract.valuation import Quarter, parse_quarter

    P = pr.current()
    want = Quarter(P.year, P.q)
    for name in pm.sheet_names:
        if name.strip().lower().startswith("key metrics") and parse_quarter(name) == want:
            return pm.sheet(name)
    raise ValueError(f"Portfolio Metrics has no 'Key Metrics' sheet for {P.label}; sheets: {pm.sheet_names}")


# ---------------------------------------------------------------------------
# the consolidated fund: Delaware + Mauritius, from the real books
# ---------------------------------------------------------------------------


def _lp_line(sr: StatementReader, code: int, which: str = "current") -> Value:
    ln = sr.balance_sheet()[code]
    return ln.current if which == "current" else ln.comparative


def consolidate(store: Store) -> tuple[Rec, dict[str, float]]:
    """Fund-level position at the period end. Every component is a source figure; every
    total is computed here from named inputs."""
    rec, _ = parse_delaware(store)
    d = lambda k: rec.vals[f"Delaware balance sheet: {k}"]  # noqa: E731
    lp = Workbook(config.lp_workpaper())
    fm = Workbook(config.fund_model())
    try:
        sr = StatementReader(lp, "LP")
        bs = sr.balance_sheet()
        n = {}
        L = lambda code: rec.value(f"LP SOFP {code} {bs[code].label} [current]", bs[code].current,  # noqa: E731
                                   as_of=pr.current().end_iso)
        mu_cash = L(8415)
        mu_audit, mu_mra = L(9330), L(9700)
        mu_accr, mu_gp, mu_cvcf = L(9000), L(9001), L(9050)
        mu_capital = L(5100)
        mu_other_assets = rec.value("LP SOFP Total Other Current Assets [current]",
                                    sr.total("SOFP", "Total Other Current Assets", 3), as_of=pr.current().end_iso)
        pv = fm.sheet("Portfolio Valuation")
        _, tr = pv_layout(pv)
        fv = rec.value("Portfolio fair value, Fund Model total (Chui)", pv.at(tr, 24, unit="USD_thousands"),
                       scale=1000, note="Fund Model 'Portfolio Valuation' fair value total, US$000s x 1000")
        cost = rec.value("Portfolio investment cost, Fund Model total", pv.at(tr, 12, unit="USD_thousands"),
                         scale=1000, note="Fund Model 'Portfolio Valuation' cost total, US$000s x 1000")

        lab = lambda k: f"Delaware balance sheet: {k}"  # noqa: E731
        n["cash"] = rec.drv("Fund cash at bank", d("Cash and Cash Equivalents") + mu_cash,
                            [lab("Cash and Cash Equivalents"), f"LP SOFP 8415 {bs[8415].label} [current]"], "Delaware + Mauritius")
        n["investments"] = fv
        n["receivables"] = rec.drv("Fund other receivables", d("Capital Call Receivable") + mu_other_assets,
                                   [lab("Capital Call Receivable"), "LP SOFP Total Other Current Assets [current]"], "Delaware + Mauritius")
        n["assets"] = rec.drv("Fund total assets", n["cash"] + fv + n["receivables"],
                              ["Fund cash at bank", "Portfolio fair value, Fund Model total (Chui)", "Fund other receivables"], "sum")
        n["audit"] = mu_audit
        n["tax"] = rec.drv("Fund taxation payable", d("Tax") + mu_mra, [lab("Tax"), f"LP SOFP 9700 {bs[9700].label} [current]"], "Delaware + Mauritius")
        n["other_liab"] = rec.drv(
            "Fund other liabilities",
            d("Management Fee Payable") + d("Due to Related Party") + mu_accr + mu_gp + mu_cvcf,
            [lab("Management Fee Payable"), lab("Due to Related Party"), "LP SOFP accruals / payable to GP / payable to CVCF"], "Delaware + Mauritius")
        n["liab"] = rec.drv("Fund total liabilities", n["audit"] + n["tax"] + n["other_liab"],
                            ["LP SOFP 9330 audit fees payable", "Fund taxation payable", "Fund other liabilities"], "sum")
        n["capital"] = rec.drv("Fund share capital (paid-in)", d("Capital Contributions") + mu_capital,
                               [lab("Capital Contributions"), f"LP SOFP 5100 {bs[5100].label} [current]"], "Delaware + Mauritius")
        n["nav"] = rec.drv("Fund net asset value", n["assets"] - n["liab"],
                           ["Fund total assets", "Fund total liabilities"], "assets - liabilities")
        n["retained"] = rec.drv("Fund retained earnings", n["nav"] - n["capital"],
                                ["Fund net asset value", "Fund share capital (paid-in)"], "NAV - paid-in capital")
        n["cost"] = cost
        n["mu_capital"], n["de_capital"] = mu_capital, d("Capital Contributions")
        return rec, n
    finally:
        lp.close()
        fm.close()


# ---------------------------------------------------------------------------
# builders that write finished tables
# ---------------------------------------------------------------------------


def build_balance_sheet(store: Store) -> str:
    P = pr.current()
    rec, n = consolidate(store)
    prior = prior_rows("balance", rec, f"{P.prev.label} report balance sheet [{P.prev.end_short}]")
    g = lambda k: pick(prior, k)[0]  # noqa: E731
    rows = [
        ["CURRENT ASSETS", "", ""],
        ["Cash at bank", money(n["cash"]), money(g("CASH AT BANK"))],
        ["Investments", money(n["investments"]), money(g("INVESTMENTS"))],
        ["Other receivables", money(n["receivables"]), money(g("OTHER RECEIVABLES"))],
        ["Total assets", money(n["assets"]), money(g("TOTAL ASSETS"))],
        ["CURRENT LIABILITIES", "", ""],
        ["Administration fees", "–", "–"],
        ["Audit fees", money(n["audit"]), money(g("AUDIT FEES"))],
        ["Taxation payable", money(n["tax"]), "–"],
        ["Other liabilities", money(n["other_liab"]), money(g("OTHER LIABILITIES"))],
        ["Total current liabilities", money(n["liab"]), money(g("TOTAL CURRENT LIABILITIES"))],
        ["CAPITAL AND RESERVES", "", ""],
        ["Share capital", money(n["capital"]), money(g("SHARE CAPITAL"))],
        ["Retained earnings", money(n["retained"]), money(g("RETAINED EARNINGS"))],
        ["Net asset value", money(n["nav"]), money(g("NET ASSET VALUE"))],
        ["Total liabilities, capital and reserves", money(n["liab"] + n["nav"]), money(g("TOTAL LIABILITIES, CAPITAL AND RESERVES"))],
    ]
    rec.drv("Fund total liabilities, capital and reserves", n["liab"] + n["nav"],
            ["Fund total liabilities", "Fund net asset value"], "sum")
    rec.save(store)
    _ensure_section(store, "4.1")
    store.set_table("t_balance_sheet", "Unaudited Balance Sheet", ["", f"{P.end_label} (USD)", f"{P.prev.end_label} (USD)"],
                    rows, "4.1", {"banner_rows": [0, 5, 11], "total_rows": [4, 10, 14, 15], "widths": [6, 3, 3]})
    return (f"4.1 balance sheet built: assets {n['assets']:,.2f}, liabilities {n['liab']:,.2f}, "
            f"NAV {n['nav']:,.2f}. Ledger labels: 'Fund cash at bank', 'Fund net asset value', "
            f"'Portfolio fair value, Fund Model total (Chui)', 'Fund share capital (paid-in)', ...")


def build_operations(store: Store, notes: list[str] | None = None) -> str:
    P = pr.current()
    L, PL = P.label, P.prev.label
    rec, n = consolidate(store)
    notes = notes if notes is not None else []
    lp = Workbook(config.lp_workpaper())
    try:
        ex = lp_quarter_expenses(lp, L, notes)
    finally:
        lp.close()
    lpname = config.lp_workpaper().name
    for code, (name, amt, cells) in ex.items():
        rec.src(f"LP GL {L} expense {code} {name}", amt, file=lpname, sheet="GL", cell=cells.split(",")[0],
                note=f"sum of GL cells {cells}")
    dm = rec.vals["Delaware income statement: Base Management Fees"]
    da = rec.vals["Delaware income statement: Fund Administration Fees"]
    mu_admin_codes = [c for c in ex if c not in (4125, 4150, 4570)]
    mu = lambda c: ex[c][1] if c in ex else 0.0  # noqa: E731
    q2 = {
        "mgmt": rec.drv(f"{L} management fee (fund)", -dm + mu(4125),
                        ["Delaware income statement: Base Management Fees", f"LP GL {L} expense 4125 Management fees"], "Delaware + Mauritius"),
        "admin": rec.drv(f"{L} administration fees (fund)", -da + sum(mu(c) for c in mu_admin_codes),
                         ["Delaware income statement: Fund Administration Fees"] + [f"LP GL {L} expense {c} {ex[c][0]}" for c in mu_admin_codes], "Delaware + Mauritius"),
        "audit": rec.drv(f"{L} audit expense (fund)", mu(4570), [f"LP GL {L} expense 4570 Audit fees"], "Mauritius"),
        "other": rec.drv(f"{L} other expense (fund)", mu(4150), [f"LP GL {L} expense 4150 Bank Charges"], "Mauritius bank charges"),
    }
    q2["total"] = rec.drv(f"{L} total investment expenses (fund)", sum(q2.values()),
                          [f"{L} {k} (fund)" for k in ("management fee", "administration fees", "audit expense", "other expense")], "sum")

    bs_prefix = f"{PL} report balance sheet [{P.prev.end_short}]"
    ops_prefix = f"{PL} report operations [QTD {P.prev.end_short}]"
    prior_rows("balance", rec, bs_prefix)                      # prior fair value
    pq = prior_rows("operations", rec, ops_prefix)
    names = {"management fee": "MANAGEMENT FEE", "administration fees": "ADMINISTRATION FEES",
             "audit expense": "AUDIT EXPENSE", "other expense": "OTHER EXPENSE",
             "total investment expenses": "TOTAL INVESTMENT EXPENSES"}
    prior_q = {k: pick(pq, v)[0] for k, v in names.items()}      # the prior quarter's own figure
    prior_ytd = {k: pick(pq, v)[-1] for k, v in names.items()}   # year to date at the prior quarter end
    # Unrealised gain = change in fair value less capital deployed in the quarter
    from ..extract.published_report import read_portfolio_performance

    prior_pos = {p.company: p.cost_usd for p in read_portfolio_performance(prior_report_path())}
    fm = Workbook(config.fund_model())
    try:
        pv = fm.sheet("Portfolio Valuation")
        company_rows, _ = pv_layout(pv)
        adds, inputs = 0.0, []
        for r in company_rows:
            name = resolve(str(pv.raw(r, 3)), strict=False)
            c_now = round(float(pv.raw(r, 12)))                      # US$000, whole thousands as published
            c_then = round(prior_pos.get(name, 0.0) / 1000)
            if c_now > c_then:
                adds += (c_now - c_then) * 1000
                inputs.append(f"{name}: {c_then} -> {c_now} (US$000)")
    finally:
        fm.close()
    additions = rec.drv(f"{L} capital deployed into portfolio companies", adds,
                        [f"Fund Model per-company cost vs {PL} report per-company cost"], "; ".join(inputs))
    fv_label, fv_prior = fact_like(rec, f"{bs_prefix}: Investments")
    unreal = rec.drv(f"{L} unrealised investment gain (fund)", n["investments"] - fv_prior - additions,
                     ["Portfolio fair value, Fund Model total (Chui)", fv_label,
                      f"{L} capital deployed into portfolio companies"], "fair value - prior fair value - capital deployed")
    net_q = rec.drv(f"{L} net profit (fund)", unreal - q2["total"],
                    [f"{L} unrealised investment gain (fund)", f"{L} total investment expenses (fund)"], "gain - expenses")
    first_quarter = P.is_first_quarter          # a year starts here: year to date is just this quarter
    ytd = {}
    for k, label_ in (("management fee", "mgmt"), ("administration fees", "admin"), ("audit expense", "audit"),
                      ("other expense", "other"), ("total investment expenses", "total")):
        cur = q2[label_]
        if first_quarter:
            ytd[k] = rec.drv(f"YTD {P.year} {k} (fund)", cur, [f"{L} {k} (fund)"], "first quarter of the year")
        else:
            ytd[k] = rec.drv(f"YTD {P.year} {k} (fund)", prior_ytd[k] + cur,
                             [f"{ops_prefix}: {names[k].title()} [YTD]", f"{L} {k} (fund)"], f"{PL} YTD + {L}")
    ukey = "UNREALISED INVESTMENT GAIN / (LOSS)"
    prior_unreal_q = pq.get(ukey, [0.0])[0]
    if first_quarter:
        ytd_unreal = rec.drv(f"YTD {P.year} unrealised investment gain (fund)", unreal,
                             [f"{L} unrealised investment gain (fund)"], "first quarter of the year")
    else:
        ytd_unreal = rec.drv(f"YTD {P.year} unrealised investment gain (fund)", pq.get(ukey, [0.0])[-1] + unreal,
                             [f"{L} unrealised investment gain (fund)"], f"{PL} YTD + {L}")
    ytd_net = rec.drv(f"YTD {P.year} net profit (fund)", ytd_unreal - ytd["total investment expenses"],
                      [f"YTD {P.year} unrealised investment gain (fund)", f"YTD {P.year} total investment expenses (fund)"], "gain - expenses")
    rec.save(store)

    def row(label, a, b, c):
        return [label, money(a), money(b), money(c)]

    rows = [
        row("Investment income", 0, 0, 0),
        ["INVESTMENT EXPENSES", "", "", ""],
        row("Management fee", prior_q["management fee"], q2["mgmt"], ytd["management fee"]),
        row("Administration fees", prior_q["administration fees"], q2["admin"], ytd["administration fees"]),
        row("Audit expense", prior_q["audit expense"], q2["audit"], ytd["audit expense"]),
        row("Other expense", prior_q["other expense"], q2["other"], ytd["other expense"]),
        row("Total investment expenses", prior_q["total investment expenses"], q2["total"], ytd["total investment expenses"]),
        ["GAIN / (LOSS) ON INVESTMENTS", "", "", ""],
        row("Unrealised investment gain / (loss)", prior_unreal_q, unreal, ytd_unreal),
        row("Net profit / (loss)", prior_unreal_q - prior_q["total investment expenses"], net_q, ytd_net),
    ]
    _ensure_section(store, "4.2")
    store.set_table("t_operations", "Unaudited Statement of Operations",
                    ["", f"{PL} ({P.prev.end_short})", f"Quarter to date ({P.end_short})", f"Year to date ({P.end_short})"],
                    rows, "4.2", {"banner_rows": [1, 7], "total_rows": [6, 9], "widths": [6, 3, 3, 3]})
    for note in notes:
        store.add_review_note("source data", note, "warning")
    return (f"4.2 statement of operations built: {L} expenses {q2['total']:,.2f}, unrealised gain {unreal:,.2f}, "
            f"net {net_q:,.2f}; YTD net {ytd_net:,.2f}.")


def build_lp_commitments(store: Store) -> str:
    rec, n = consolidate(store)
    fm = Workbook(config.fund_model())
    try:
        sr = fm.sheet("Summary Report")
        at = summary_layout(sr)

        def cell(label: str, key: str, expect: str | None = None) -> float:
            r = at[key]
            if expect and str(sr.raw(r, 3)).strip() != expect:
                raise ValueError(f"Summary Report C{r} is {sr.raw(r, 3)!r}, expected {expect!r}: "
                                 f"the commitments block has changed shape")
            return rec.value(label, sr.at(r, 4, unit="USD"))

        hnwi = cell("Commitment: HNWIs", "hnwi", "HNWIs")
        fo = cell("Commitment: Family Offices", "fo", "Family Offices")
        msdf = cell("Commitment: Michael & Susan Dell Foundation", "msdf", "Michael & Susan Dell Foundation")
        meda_eq = cell("Commitment: MEDA Foundation (Equalization), Delaware", "meda_eq", "MEDA Mauritius Foundation")
        de_total = cell("Commitment: Delaware total", "de_total", "Total")
        mu = cell("Commitment: MEDA Mauritius Foundation, Mauritius", "mu", "MEDA Mauritius Foundation")
        equity = cell("Commitment: total equity", "equity", "TOTAL")
        debt = cell("Commitment: MEDA Debt Fund", "debt", "MEDA Debt Fund")
        grand = cell("Commitment: grand total", "grand", "TOTAL")
    finally:
        fm.close()
    de_called, mu_called = n["de_capital"], n["mu_capital"]
    de_rem = max(0.0, de_total - de_called)
    mu_rem = rec.drv("Remaining commitment: Mauritius equity", mu - mu_called,
                     ["Commitment: MEDA Mauritius Foundation, Mauritius", "Fund share capital (paid-in)"], "commitment - called")
    eq_called = rec.drv("Called capital: total equity", de_called + mu_called,
                        ["Fund share capital (paid-in)"], "Delaware + Mauritius")
    eq_rem = rec.drv("Remaining commitment: total equity", de_rem + mu_rem, ["Remaining commitment: Mauritius equity"], "sum")
    gr_rem = rec.drv("Remaining commitment: grand total", eq_rem + debt, ["Remaining commitment: total equity", "Commitment: MEDA Debt Fund"], "sum")
    share = lambda lab, v: rec.drv(f"Share of equity commitments: {lab}", v / equity,  # noqa: E731
                                   [f"Commitment: {lab}", "Commitment: total equity"], "commitment / equity total", "ratio")
    s = {"HNWIs": share("HNWIs", hnwi), "Family Offices": share("Family Offices", fo),
         "Michael & Susan Dell Foundation": share("Michael & Susan Dell Foundation", msdf),
         "MEDA Foundation (Equalization), Delaware": share("MEDA Foundation (Equalization), Delaware", meda_eq),
         "Delaware total": share("Delaware total", de_total),
         "MEDA Mauritius Foundation, Mauritius": share("MEDA Mauritius Foundation, Mauritius", mu)}
    rec.save(store)
    rows = [
        ["Chui Ventures Fund I LP — Delaware", "", "", "", "", ""],
        ["HNWIs", "Angel Investors", money(hnwi), "", "", pct1(s["HNWIs"])],
        ["Family Offices", "Family Office", money(fo), "", "", pct1(s["Family Offices"])],
        ["Michael & Susan Dell Foundation", "Impact Investor", money(msdf), "", "", pct1(s["Michael & Susan Dell Foundation"])],
        ["MEDA Foundation (Equalization)", "Impact Investor", money(meda_eq), "", "", pct1(s["MEDA Foundation (Equalization), Delaware"])],
        ["Delaware sub-total", "", money(de_total), money(de_called), money(de_rem), pct1(s["Delaware total"])],
        ["Chui Ventures LP — Mauritius (Equity)", "", "", "", "", ""],
        ["MEDA Mauritius Foundation", "Impact Investor", money(mu), money(mu_called), money(mu_rem), pct1(s["MEDA Mauritius Foundation, Mauritius"])],
        ["Equity sub-total", "", money(equity), money(eq_called), money(eq_rem), "100.0%"],
        ["Chui Ventures LP — Mauritius (Debt Fund)", "", "", "", "", ""],
        ["MEDA Debt Fund", "DFI / Debt Provider", money(debt), "–", money(debt), ""],
        ["Grand total", "", money(grand), money(eq_called), money(gr_rem), ""],
    ]
    _ensure_section(store, "2.2")
    store.set_table("t_lp_commitments", f"LP Commitments & Capital Accounts — {pr.current().end_label}",
                    ["Investor", "Type of investor", "Total commitment (USD)", "Called capital (USD)",
                     "Remaining commitment (USD)", "% of equity commitments"], rows, "2.2",
                    {"banner_rows": [0, 6, 9], "total_rows": [5, 8, 11], "widths": [6.5, 4.2, 3.2, 3.2, 3.4, 2.6],
                     "align": ["l", "l", "r", "r", "r", "r"]})
    return (f"2.2 LP commitments built: equity {equity:,.0f}, called {eq_called:,.2f}, remaining {eq_rem:,.2f}, "
            f"grand total {grand:,.0f}.")


def _kfact(rec: Rec, label: str, usd: float, inputs: list[str], formula: str = "USD / 1000") -> float:
    return rec.drv(label, usd / 1000.0, inputs, formula, unit="USD_thousands")


def build_fund_summary(store: Store) -> str:
    rec, n = consolidate(store)
    fm = Workbook(config.fund_model())
    try:
        sr, asm = fm.sheet("Summary Report"), fm.sheet("Assumptions")
        committed = rec.value("Commitment: total equity (summary)", sr.at(summary_layout(sr)["equity"], 4, unit="USD"))
        fee_row = _label_row(asm, "Management Fees, per year")
        fee_v = asm.at(fee_row, 4)
        fee_rate = rec.src("Management fee load over fund life", fee_v.raw, file=config.fund_model().name,
                           sheet="Assumptions", cell=fee_v.provenance.cell, unit="ratio",
                           note="share of committed capital charged in fees over the fund life")
        costs = rec.value("Fund costs (organisational)", asm.at(_label_row(asm, "Organizational Expenses"), 6, unit="USD"))
    finally:
        fm.close()
    fees = rec.drv("Management fees over 10 years", committed * fee_rate,
                   ["Commitment: total equity (summary)", "Management fee load over fund life"], "commitment x rate")
    invest = rec.drv("Total investable capital", committed - fees - costs,
                     ["Commitment: total equity (summary)", "Management fees over 10 years", "Fund costs (organisational)"], "commitment - fees - costs")
    fv, nav, paid, cost = n["investments"], n["nav"], n["capital"], n["cost"]
    other = rec.drv("Total other assets and liabilities (net)", nav - fv,
                    ["Fund net asset value", "Portfolio fair value, Fund Model total (Chui)"], "NAV - fair value")
    planned = rec.drv("Total additional planned for investments", invest - cost,
                      ["Total investable capital", "Portfolio investment cost, Fund Model total"], "investable - invested")
    tvpi = rec.drv("TVPI", nav / paid, ["Fund net asset value", "Fund share capital (paid-in)"], "(NAV + distributions) / paid-in", "ratio")
    rvpi = rec.drv("RVPI", nav / paid, ["Fund net asset value", "Fund share capital (paid-in)"], "NAV / paid-in", "ratio")
    dpi = rec.drv("DPI", 0.0, [], "no distributions made", "ratio")
    pic = rec.drv("Paid-in to committed", paid / committed, ["Fund share capital (paid-in)", "Commitment: total equity (summary)"], "paid-in / committed", "ratio")
    items = {"committed": committed, "fees": fees, "costs": costs, "invest": invest, "fv": fv, "other": other,
             "nav": nav, "paid": paid, "cost": cost, "planned": planned}
    k = {name: _kfact(rec, f"{name.title()} (US$000)", v, [name]) for name, v in items.items()}
    pc = lambda lab, v, base_label, base: rec.drv(f"{lab} as % of {base_label}", v / base, [lab], f"/ {base_label}", "ratio")  # noqa: E731
    pct_c = {name: rec.drv(f"{name.title()} as % of committed", v / committed, [f"{name.title()} (US$000)"], "/ committed", "ratio")
             for name, v in items.items() if name not in ("planned", "cost")}
    pct_i = {name: rec.drv(f"{name.title()} as % of investable", items[name] / invest, [f"{name.title()} (US$000)"], "/ investable", "ratio")
             for name in ("cost", "planned", "invest")}
    rec.save(store)
    x = lambda v: f"{v:.2f}x"  # noqa: E731
    rows = [
        ["FUND SIZE & DEPLOYMENT", "", ""],
        ["Total committed equity capital", n0(k["committed"]), pct1(pct_c["committed"])],
        ["Less management fees (10 years)", n0(k["fees"]), pct1(pct_c["fees"])],
        ["Less fund costs", n0(k["costs"]), pct1(pct_c["costs"])],
        ["Total investable capital", n0(k["invest"]), pct1(pct_c["invest"])],
        ["Cumulative distributions", "–", "0.0%"],
        ["Fair value of portfolio", n0(k["fv"]), pct1(pct_c["fv"])],
        ["Total other assets and liabilities (net)", n0(k["other"]), pct1(pct_c["other"])],
        ["Total net asset value", n0(k["nav"]), pct1(pct_c["nav"])],
        ["PERFORMANCE METRICS", "", ""],
        ["Paid-in capital", n0(k["paid"]), pct1(pct_c["paid"])],
        ["Multiple to investors (TVPI)", x(tvpi), ""],
        ["DPI", x(dpi), ""],
        ["RVPI", x(rvpi), ""],
        ["Paid-in to committed capital", pct1(pic), ""],
        ["PORTFOLIO ALLOCATION (% of investable capital)", "", ""],
        ["Total invested in portfolio companies", n0(k["cost"]), pct1(pct_i["cost"])],
        ["Total additional planned for investments", n0(k["planned"]), pct1(pct_i["planned"])],
        ["Total allocated to portfolio companies", n0(k["invest"]), pct1(pct_i["invest"])],
    ]
    _ensure_section(store, "2.1")
    store.set_table("t_fund_summary", "Fund Summary", ["", f"{pr.current().end_label} (US$'000)", "% of committed capital"],
                    rows, "2.1", {"banner_rows": [0, 9, 15], "total_rows": [8, 18], "widths": [8, 2.4, 2.6]})
    return (f"2.1 fund summary built: NAV {nav:,.2f} (US$000 {k['nav']:,.1f}), TVPI {tvpi:.3f}x, "
            f"paid-in {paid:,.2f}, investable {invest:,.0f}.")


# ---------------------------------------------------------------------------
# portfolio: 5.1 sector, 5.2 region, 5.3 composition, 5.4 performance summary
# ---------------------------------------------------------------------------


def _display_name(raw: str) -> str:
    canon = resolve(raw, strict=False)
    return {"PaidHR": "PaidHR", "Lami": "Lami", "MightyFin": "Mighty Finance"}.get(canon or "", canon or raw.strip())


def build_portfolio(store: Store) -> str:
    rec = Rec()
    fm = Workbook(config.fund_model())
    fname = config.fund_model().name
    try:
        pv = fm.sheet("Portfolio Valuation")
        company_rows, totals_row = pv_layout(pv)
        cos = []
        for r in company_rows:
            name = _display_name(str(pv.raw(r, 3)))
            c = {"name": name, "sector": str(pv.raw(r, 4)).strip(), "region": str(pv.raw(r, 6)).strip(),
                 "date": pv.raw(r, 7)}
            for key, col, unit in (("fd", 11, "ratio"), ("cost", 12, "USD_thousands"), ("uncost", 20, "USD_thousands"),
                                   ("fv100", 23, "USD_thousands"), ("fvchui", 24, "USD_thousands"), ("mult", 26, "ratio")):
                v = pv.at(r, col, unit=unit)
                rec.src(f"{name} {key}", float(v.raw), file=fname, sheet="Portfolio Valuation",
                        cell=v.provenance.cell, unit=unit)
                c[key] = float(v.raw)
            cos.append(c)
        tot = {}
        for key, col, unit in (("cost", 12, "USD_thousands"), ("uncost", 20, "USD_thousands"),
                               ("fv100", 23, "USD_thousands"), ("fvchui", 24, "USD_thousands"), ("mult", 26, "ratio")):
            v = pv.at(totals_row, col, unit=unit)
            tot[key] = rec.src(f"Portfolio total {key}", float(v.raw), file=fname, sheet="Portfolio Valuation",
                               cell=v.provenance.cell, unit=unit)
    finally:
        fm.close()

    # 5.4 performance summary
    rows = []
    for i, c in enumerate(cos, start=1):
        d = c["date"]
        rows.append([str(i), c["name"], c["sector"], c["region"], d.strftime("%b-%Y") if hasattr(d, "strftime") else "",
                     pct1(c["fd"]), n0(c["cost"]), n0(c["uncost"]), n0(c["fv100"]), n0(c["fvchui"]), f"{hu(c['mult'], 2)}x"])
    rows.append(["", "Total", "", "", "", "", n0(tot["cost"]), n0(tot["uncost"]), n0(tot["fv100"]),
                 n0(tot["fvchui"]), f"{hu(tot['mult'], 2)}x"])
    _ensure_section(store, "5.4")
    store.set_table("t_portfolio_summary", "Portfolio Performance Summary",
                    ["#", "Company", "Sector", "Region", "Initial investment date", "% FD",
                     "Investment cost US$000's", "Unrealized cost", "Fair value (100%)", "Fair value (Chui)",
                     "Unrealized multiple"], rows, "5.4",
                    {"landscape": True, "total_rows": [-1], "widths": [2, 8, 12, 8, 7, 4.5, 6.5, 6, 7, 7, 6.5],
                     "align": ["c", "l", "l", "l", "l", "r", "r", "r", "r", "r", "r"]})

    # 5.1 sector and 5.2 region
    def breakdown(group: str, key: str, label: str, kind: str, chart_title: str):
        agg: dict[str, list[float]] = {}
        for c in cos:
            a = agg.setdefault(c[group], [0, 0.0])
            a[0] += 1
            a[1] += c["cost"]
        total = sum(v[1] for v in agg.values())
        rec.drv(f"{label} breakdown total invested (US$000)", total,
                [f"{c['name']} cost" for c in cos], "sum", unit="USD_thousands")
        order = sorted(agg.items(), key=lambda kv: -kv[1][1])
        trows, labels, values = [], [], []
        for name, (cnt, amt) in order:
            rec.drv(f"{label} {name} invested capital (US$000)", amt,
                    [f"{c['name']} cost" for c in cos if c[group] == name], "sum", unit="USD_thousands")
            share = rec.drv(f"{label} {name} share of invested capital", amt / total,
                            [f"{label} {name} invested capital (US$000)"], "/ total", "ratio")
            trows.append([name, str(cnt), pct1(share), n0(amt)])
            labels.append(name)
            values.append(amt)
        trows.append(["Total", str(len(cos)), "100.0%", n0(total)])
        sec = {"sector": "5.1", "region": "5.2"}[group]
        _ensure_section(store, sec)
        store.set_table(f"t_{group}", SECTION_TITLES[sec], [label, "No. of companies",
                        "% of portfolio by invested capital", "Invested capital (US$000's)"],
                        trows, sec, {"total_rows": [-1], "widths": [5, 3, 4, 3]})
        store.set_chart(f"c_{group}", chart_title, kind, labels, values, sec, {"height_in": 2.7})

    breakdown("sector", "sector", "Sector", "donut", "Portfolio by sector — invested capital (US$000's)")
    breakdown("region", "region", "Country / region", "donut", "Portfolio by country — invested capital (US$000's)")

    # 5.3 composition by company
    comp = sorted(cos, key=lambda c: -c["cost"])
    _ensure_section(store, "5.3")
    store.set_chart("c_composition", "Amount invested by portfolio company (US$000's)", "hbar",
                    [c["name"] for c in comp], [c["cost"] for c in comp], "5.3", {"height_in": 5.2})
    rec.save(store)
    return (f"portfolio built: {len(cos)} companies; total cost US$000 {tot['cost']:,.1f}, "
            f"fair value (Chui) US$000 {tot['fvchui']:,.1f}, multiple {tot['mult']:.3f}x. "
            f"Sections 5.1, 5.2, 5.3 (charts) and 5.4 (landscape table) written.")


# ---------------------------------------------------------------------------
# 5.5 jobs & impact
# ---------------------------------------------------------------------------

_JOB_COLS = [("Total jobs", "Total Jobs"), ("Direct jobs", "Direct Jobs"), ("Indirect jobs", "Indirect Jobs"),
             ("Female jobs", "Female Total Jobs"), ("Youth in work", "Youth In Work in the Workforce"),
             ("Young women in work", "Youth In Work( (Female)"), ("Earning below $300 / month", "No. of People Earning Below")]


def build_jobs(store: Store) -> str:
    rec = Rec()
    pm = Workbook(config.portfolio_metrics())
    try:
        sh = metrics_sheet(pm)
        hdr_row = next(r for r in range(1, 60) if str(sh.raw(r, 1)).strip() == "Company" and sh.raw(r, 2) == "Total Jobs")
        header = {c: str(sh.raw(hdr_row, c) or "").replace("\n", " ").strip() for c in range(1, 25)}
        col = {}
        for label, prefix in _JOB_COLS:
            col[label] = next((c for c, h in header.items() if h.lower().startswith(prefix.lower())), None)
        fin = next((c for c, h in header.items() if "financial inclusion" in h.lower()), None)
        use = [(lab, c) for lab, c in col.items() if c]
        if fin:
            use.append(("Financial inclusion", fin))
        rows, fname = [], config.portfolio_metrics().name
        r = hdr_row + 1
        while True:
            name = sh.raw(r, 1)
            if name is None:
                break
            is_total = str(name).strip().lower() == "total"
            disp = "Total" if is_total else _display_name(str(name))
            cells = []
            for lab, c in use:
                v = sh.raw(r, c)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    rec.src(f"Jobs & impact: {disp} {lab}", float(v), file=fname, sheet=sh.name,
                            cell=sh.addr(r, c), unit="count")
                    cells.append(f"{hu(float(v)):,}" if v else "–")
                elif isinstance(v, str) and (m := re.fullmatch(r"([\d,]+)(\+?)", v.strip())):
                    rec.src(f"Jobs & impact: {disp} {lab}", float(m.group(1).replace(",", "")), file=fname,
                            sheet=sh.name, cell=sh.addr(r, c), unit="count")
                    cells.append(v.strip())
                else:
                    cells.append("–")
            rows.append([("" if is_total else str(len(rows) + 1)), disp] + cells)
            if is_total:
                break
            r += 1
    finally:
        pm.close()
    rows = [[str(i) if (row[0] and i) else "", *row[1:]] for i, row in enumerate(rows, 1)]
    for i, row in enumerate(rows):
        if row[1] == "Total":
            row[0] = ""
    rec.save(store)
    _ensure_section(store, "5.5")
    store.set_table("t_jobs", "Jobs & Impact Metrics", ["#", "Company"] + [lab for lab, _ in use], rows, "5.5",
                    {"landscape": True, "total_rows": [-1], "widths": [2, 8] + [5] * len(use),
                     "align": ["c", "l"] + ["r"] * len(use)})
    return f"5.5 jobs & impact built: {len(rows) - 1} companies, columns {[lab for lab, _ in use]}."
