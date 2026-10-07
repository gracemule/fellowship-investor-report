"""Turn the agent's tool calls into sentences a person can follow.

The narration is built from what the agent actually did -- never from what the model says it
did -- so it cannot drift from reality or invent progress. Each tool belongs to a chapter, and
the interface groups the timeline by chapter, which is what makes a 60-step run readable as
five things rather than sixty.
"""

from __future__ import annotations

import re

CHAPTERS = ("Reading sources", "Recording figures", "Building tables", "Writing", "Checking and rendering")

_C = {
    "list_sources": CHAPTERS[0], "read_pdf": CHAPTERS[0], "read_text": CHAPTERS[0], "look_at_image": CHAPTERS[0], "web_search": CHAPTERS[0], "web_fetch": CHAPTERS[0], "research_macro": CHAPTERS[0], "delegate_research": CHAPTERS[0], "build_macro_table": CHAPTERS[2], "excel_sheets": CHAPTERS[0],
    "excel_find_value": CHAPTERS[0], "excel_dump_region": CHAPTERS[0], "portfolio_valuations": CHAPTERS[0],
    "fund_capital_position": CHAPTERS[0], "financial_statements": CHAPTERS[0], "prior_report_table": CHAPTERS[0],
    "report_save_facts": CHAPTERS[1], "report_derive_fact": CHAPTERS[1], "ledger_search": CHAPTERS[1],
    "report_review_note": CHAPTERS[1], "report_remove_review_note": CHAPTERS[1], "list_review_notes": CHAPTERS[1],
    "report_resolve_review_note": CHAPTERS[1], "prior_quarter_report": CHAPTERS[0],
    "build_fund_tables": CHAPTERS[2], "build_portfolio_tables": CHAPTERS[2], "report_set_table": CHAPTERS[2],
    "report_set_cover": CHAPTERS[3], "report_set_section": CHAPTERS[3], "report_remove_section": CHAPTERS[3],
    "report_outline": CHAPTERS[4], "report_render": CHAPTERS[4], "inspect_pages": CHAPTERS[4],
    "ask_user": CHAPTERS[3], "request_sources": CHAPTERS[0],
}


def _s(v, n=60) -> str:
    return str(v).strip().replace("\n", " ")[:n]


def _host(url) -> str:
    from urllib.parse import urlparse

    h = urlparse(str(url or "")).hostname or "a web page"
    return h.removeprefix("www.")


_EXT = re.compile(r"\.(xlsx|xlsm|xls|pdf|docx|csv|txt|md|json|png|jpe?g|webp|gif)$", re.I)


def friendly_file(name: str) -> str:
    """A file name as a person would say it: no extension, no list number, no copy suffix, 'LAMI valuation report'."""
    s = _EXT.sub("", name.strip())
    s = re.sub(r"^\d+\s*[.)]\s*", "", s)
    s = re.sub(r"\s*\(\d+\)\s*$", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if re.search(r"valuation report", s, re.I):
        company = re.split(r"\s*[-–]\s*|\s+valuation", s, maxsplit=1, flags=re.I)[0].strip()
        if company and company.casefold() != "valuation report":
            return f"{company} valuation report"
    return s


def _file(args: dict, names=()) -> str:
    f = str(args.get("file_name") or "").rsplit("/", 1)[-1]
    if names and f:
        low = f.casefold()
        hit = next((x for x in names if x.casefold() == low), None) or next((x for x in names if low in x.casefold()), None)
        if hit:
            return friendly_file(hit)
    return f.rsplit(".", 1)[0] if len(f) > 3 else f


def describe_call(name: str, args: dict, names=()) -> tuple[str, str]:
    """(chapter, present-tense sentence). `names` are the file names in the sources, so a partial name the agent used is said in full."""
    a = args or {}
    _file_ = lambda x: _file(x, names)  # noqa: E731
    ch = _C.get(name, CHAPTERS[0])
    text = {
        "list_sources": "Checking which source documents are available",
        "read_pdf": f"Reading {_file_(a) or 'a PDF'}",
        "read_text": f"Reading {_file_(a) or 'a document'}",
        "look_at_image": f"Looking at {_file_(a) or 'an image'}",
        "research_macro": f"Researching {len(a.get('countries') or [])} countries’ macro data with subagents",
        "delegate_research": f"Delegating a research question: {_s(a.get('label') or a.get('question'), 60)}",
        "build_macro_table": "Building the macro table from verified figures",
        "web_search": f"Searching the web for “{_s(a.get('query'), 70)}”",
        "web_fetch": f"Reading {_host(a.get('url'))}",
        "excel_sheets": f"Opening {_file_(a) or 'a workbook'}",
        "excel_find_value": f"Looking up “{_s(a.get('label'), 40)}” in {_file_(a) or 'a workbook'}",
        "excel_dump_region": f"Reading the {_s(a.get('sheet'), 40)} sheet of {_file_(a) or 'a workbook'}",
        "portfolio_valuations": "Reading every company’s valuation and reconciling the two sources",
        "fund_capital_position": "Working out the Fund’s capital position",
        "financial_statements": "Reading the financial statements",
        "prior_report_table": "Reading the previous report for comparatives",
        "report_save_facts": "Recording figures with their sources",
        "report_derive_fact": f"Calculating {_s(a.get('label'), 60)}" if a.get("label") else "Calculating a figure",
        "ledger_search": f"Looking up recorded figures for “{_s(a.get('text'), 40)}”",
        "report_review_note": "Noting a data issue for your review",
        "report_remove_review_note": "Removing a review note that no longer applies",
        "list_review_notes": "Checking the open review notes",
        "report_resolve_review_note": "Closing a review note that is now settled",
        "prior_quarter_report": "Looking back at the previous quarter’s report",
        "build_fund_tables": "Building the fund summary, commitments and financial statements",
        "build_portfolio_tables": "Building the portfolio tables and charts",
        "report_set_table": f"Setting up {_s(a.get('title') or a.get('key'), 50)}",
        "report_set_cover": "Setting up the cover page",
        "report_set_section": f"Writing {_s(a.get('key'), 10)} {_s(a.get('title'), 50)}".strip(),
        "report_remove_section": f"Removing section {_s(a.get('key'), 10)}",
        "report_outline": "Reviewing the report outline",
        "report_render": "Rendering the report",
        "inspect_pages": f"Inspecting page{'s' if ',' in str(a.get('pages', '')) or '-' in str(a.get('pages', '')) else ''} "
                         f"{_s(a.get('pages') or '1', 20)} of the rendered report",
        "ask_user": "Asking you a question",
        "request_sources": "Asking for missing source documents",
    }.get(name, f"Running {name.replace('_', ' ')}")
    return ch, text


def describe_result(name: str, content: str, status: str | None = None) -> tuple[str, str | None]:
    """(level, text). level is 'ok' or 'issue'. Text is only given when it adds something."""
    body = str(content or "").strip()
    first = body.splitlines()[0] if body else ""
    if status == "error" or first.startswith(("ERROR", "RENDER BLOCKED")) or body.startswith("Error"):
        msg = first.removeprefix("RENDER BLOCKED:").removeprefix("ERROR:").removeprefix("ERROR").strip()
        if first.startswith("RENDER BLOCKED"):
            return "issue", "The render was blocked by a check, and the agent is correcting it: " + _s(msg, 180)
        return "issue", _s(msg, 200) or "That step failed; the agent is adjusting."
    if name == "report_render":
        return "ok", first.capitalize() if first else None
    if name in ("report_save_facts", "build_fund_tables", "build_portfolio_tables", "report_derive_fact"):
        return "ok", _s(first, 140) or None
    if name == "inspect_pages":
        return "ok", None
    return "ok", None
