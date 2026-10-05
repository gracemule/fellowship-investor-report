"""Source-document locations.

The LP source data lives outside the repo on purpose -- it is confidential and
must never be committed. SOURCE_ROOT points at the directory holding the
"Fund Financials", "Valuation Reports" etc. folders, and is overridable so the
same code runs against a fixture set in CI.
"""

from __future__ import annotations

import os
from pathlib import Path

SOURCE_ROOT = Path(
    os.environ.get("CHUI_SOURCE_ROOT", Path(__file__).resolve().parents[3])
).resolve()

FUND_FINANCIALS = SOURCE_ROOT / "Fund Financials"
VALUATION_REPORTS = SOURCE_ROOT / "Valuation Reports"
PORTFOLIO_DATA = SOURCE_ROOT / "Portfolio Company Data"
PRIOR_PERIOD = SOURCE_ROOT / "Prior Period Baseline"
PIPELINE = SOURCE_ROOT / "Pipeline and subsequent events"
MACRO = SOURCE_ROOT / "Macro and Context"
GP_STATEMENT = SOURCE_ROOT / "GP statement"
BRANDING = SOURCE_ROOT / "Branding"


def set_root(root: Path | str) -> Path:
    """Point every source location at `root`.

    In production the root is the materialised copy of the user's synced folder, which is
    replaced between runs. Callers always read these as `config.X` (attribute access, not
    `from config import X`), so rebinding the module globals moves all of them at once.
    """
    global SOURCE_ROOT, FUND_FINANCIALS, VALUATION_REPORTS, PORTFOLIO_DATA, PRIOR_PERIOD
    global PIPELINE, MACRO, GP_STATEMENT, BRANDING
    global LP_WORKPAPER, GP_WORKPAPER, FUND_MODEL, PORTFOLIO_METRICS, DELAWARE_PACKAGE, REPORT_TEMPLATE
    SOURCE_ROOT = Path(root).resolve()
    FUND_FINANCIALS = SOURCE_ROOT / "Fund Financials"
    VALUATION_REPORTS = SOURCE_ROOT / "Valuation Reports"
    PORTFOLIO_DATA = SOURCE_ROOT / "Portfolio Company Data"
    PRIOR_PERIOD = SOURCE_ROOT / "Prior Period Baseline"
    PIPELINE = SOURCE_ROOT / "Pipeline and subsequent events"
    MACRO = SOURCE_ROOT / "Macro and Context"
    GP_STATEMENT = SOURCE_ROOT / "GP statement"
    BRANDING = SOURCE_ROOT / "Branding"
    LP_WORKPAPER, GP_WORKPAPER = lp_workpaper(), gp_workpaper()
    FUND_MODEL, PORTFOLIO_METRICS = fund_model(), portfolio_metrics()
    DELAWARE_PACKAGE, REPORT_TEMPLATE = delaware_package(), report_template()
    return SOURCE_ROOT


def find(folder: Path, *patterns: str, newest: bool = True) -> Path:
    """The file in `folder` matching any glob pattern (case-insensitive), preferring the most
    recently modified. Files are found by what they are, not by their exact name: the same
    workpaper arrives as "... LP (3).xlsx" one quarter and "... LP v2.xlsx" the next."""
    hits: list[Path] = []
    if folder.exists():
        names = {p.name.casefold(): p for p in folder.iterdir() if p.is_file() and not p.name.startswith(("~$", "."))}
        import fnmatch

        for pat in patterns:
            hits += [p for n, p in names.items() if fnmatch.fnmatch(n, pat.casefold())]
    if not hits:
        return folder / patterns[0].replace("*", "")      # a path that does not exist; require() reports it
    return max(hits, key=lambda p: p.stat().st_mtime) if newest else sorted(hits)[0]


def lp_workpaper() -> Path:
    return find(FUND_FINANCIALS, "*chui ventures lp*.xlsx", "*lp*.xlsx")


def gp_workpaper() -> Path:
    return find(FUND_FINANCIALS, "*chui ventures gp*.xlsx", "*gp*.xlsx")


def fund_model() -> Path:
    return find(PORTFOLIO_DATA, "fund model*.xlsx")


def portfolio_metrics() -> Path:
    return find(PORTFOLIO_DATA, "*portfolio metrics*.xlsx")


def delaware_package() -> Path:
    return find(FUND_FINANCIALS, "*financial package*.pdf", "*fund i, lp*.pdf")


def report_template() -> Path:
    return find(SOURCE_ROOT, "*reporting template*.docx")


def prior_report(period) -> Path:
    """The published report for `period` (normally the previous quarter)."""
    return find(PRIOR_PERIOD, f"*{period.label}*report*.pdf", f"*q{period.q}*{period.year}*.pdf")


# Module-level names kept for the code written before these were functions.
LP_WORKPAPER = lp_workpaper()
GP_WORKPAPER = gp_workpaper()
FUND_MODEL = fund_model()
PORTFOLIO_METRICS = portfolio_metrics()
DELAWARE_PACKAGE = delaware_package()
REPORT_TEMPLATE = report_template()


def require(path: Path) -> Path:
    if not path.exists():
        raise FileNotFoundError(
            f"source document missing: {path}\n"
            f"Set CHUI_SOURCE_ROOT if the data lives elsewhere (currently {SOURCE_ROOT})."
        )
    return path
