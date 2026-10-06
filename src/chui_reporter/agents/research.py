"""The web researcher: finds published figures and brings back proof.

It is a worker, not an author. It searches and reads; it hands back claims (a figure, the page it came from, and
the exact sentence containing it). Code, not the model, then checks each quotation against the stored page and
writes the verified figures into the fact ledger. Unverified claims become gaps."""

from __future__ import annotations

import re
from datetime import date

from langchain_core.tools import tool

from ..agent.store import Fact, Store
from ..agent.web_tools import WEB_TOOLS
from ..services.web import verify_web_claim
from . import engine
from .engine import Context, Outcome, ResearchResult, Subagent, run_subagent

_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def periods(as_of: str) -> list[date]:
    """The first day of every period an as_of label names ('Jun 2026', 'June 2026', '2026-06', 'Q1 2026'), in the order found."""
    s = as_of.strip().casefold()
    found: list[tuple[int, date]] = []
    for m in re.finditer(r"\bq([1-4])\s*(20\d\d)\b", s):
        found.append((m.start(), date(int(m.group(2)), 3 * (int(m.group(1)) - 1) + 1, 1)))
    for m in re.finditer(r"\b([a-z]{3})[a-z]*\.?\s*(20\d\d)\b", s):
        if m.group(1) in _MONTHS:
            found.append((m.start(), date(int(m.group(2)), _MONTHS[m.group(1)], 1)))
    for m in re.finditer(r"\b(20\d\d)-(0[1-9]|1[0-2])\b", s):
        found.append((m.start(), date(int(m.group(1)), int(m.group(2)), 1)))
    return [d for _, d in sorted(found)]


def period_start(as_of: str) -> date | None:
    """The first period an as_of label names, if it can be read."""
    found = periods(as_of)
    return found[0] if found else None


def after(as_of: str, quarter_end: date | None) -> bool:
    """True if the label names any period that starts after the quarter end: a figure the report is not about. Fails
    closed: 'August 2026 (latest month published; Q2 2026 ended June 2026)' is late, because August is named."""
    return bool(quarter_end and any(d > quarter_end for d in periods(as_of)))


@tool
def verify_figure(source_url: str, quote: str, value: float) -> str:
    """Check a figure BEFORE you report it: is this quotation on the page you fetched, and does it contain this value?
    Costs nothing. If it fails, fetch the page again with web_fetch and copy the sentence exactly, or drop the figure."""
    ctx = engine.get_context()
    if ctx is None:
        return "ERROR: nothing to check against"
    ok, why = verify_web_claim(Fact(label="check", value=value, source_file=source_url, source_cell=quote, unit="x"), ctx.store)
    return ("OK: this quotation is on the page and contains the figure. You may report it." if ok else f"NOT VERIFIED: {why}")

RESEARCHER_PROMPT = """You are a research assistant for the reporting team of an investment fund. You find specific published figures \
and report them with proof. You work alone on one task and report back once.

HOW TO WORK
- Go to the publisher's own pages first: the central bank, the national statistics office, the ministry of finance, the regulator. \
Use web_search with only_sites when you know their domains. Press releases and statistical bulletins published as PDFs read very well.
- Open a page with web_fetch before relying on it. A search result is only a lead, never evidence: a figure you saw only in a \
search result or on a page you did not fetch will be thrown away. On a long page use find= to jump to a figure.
- Never rely on social media posts, forums or personal blogs. A data aggregator may point you to the right publisher but the \
figure must come from the publisher's own page.
- Be economical: your searches and page reads are limited and shared. Do not repeat a search. Stop as soon as the task is answered.
- If a page cannot be read, try the publisher's PDF or another page of theirs once; then record a gap.
- Before you finish, call verify_figure for every claim (it is free). Fix the quotation or drop the claim if it fails.

WHAT A RESULT IS
- A claim is one figure: its value as a plain number, its unit, the period it describes (as_of), the exact address of the page you \
fetched, and a quote: the exact sentence or table row from that page, copied character for character from the text you were shown, \
that contains the figure. A program checks every quote against the page; an approximate or reconstructed quote is rejected and the \
figure is lost.
- Use the period the task names: the latest figure for a period that ends on or before the date given. Do NOT use a figure for a later \
month or quarter even if it has since been published. If nothing is published for that period, report the latest earlier one.
- as_of is only the period, short, like "Jun 2026" or "Q1 2026". Put qualifications (monthly average, year on year, set at the May \
meeting) in basis.
- If you cannot find or verify a figure, do not guess. List it under gaps with the reason.

Reply with ONLY a JSON object, with no text before or after it:
{"summary": "one or two sentences", "claims": [{"key": "...", "label": "...", "value": 0.0, "unit": "...", "as_of": "Jun 2026", "basis": "...", \
"source_url": "...", "quote": "..."}], "gaps": [{"key": "...", "reason": "..."}]}"""

RESEARCHER = Subagent(
    name="web_researcher",
    title="Researcher",
    prompt=RESEARCHER_PROMPT,
    tools=tuple(WEB_TOOLS) + (verify_figure,),
    max_rounds=26,
    budgets={"web_search": 8, "web_fetch": 16},
)


def record_claims(store: Store, result: ResearchResult, *, group: str, quarter_end: date | None = None) -> dict:
    """Verify each claim against its stored page and record what holds. Returns {'verified': {key: {...}}, 'gaps': {key: why}}."""
    verified: dict[str, dict] = {}
    gaps: dict[str, str] = {g.key: g.reason for g in result.gaps}
    facts: list[Fact] = []
    for c in result.claims:
        if after(c.as_of, quarter_end):                 # a period the report is not about: never recorded
            gaps.setdefault(c.key, f"only a figure for {c.as_of} was found, which is after the quarter this report covers")
            continue
        label = f"{c.label} ({c.as_of})"
        fact = Fact(label=label, value=c.value, unit=c.unit, source_file=c.source_url, source_cell=c.quote,
                    as_of=c.as_of, note=f"found by a researcher ({group})")
        ok, why = verify_web_claim(fact, store)
        fact.status = "extracted" if ok else "claimed"
        fact.note += (" | verified: " if ok else " | UNVERIFIED: ") + why
        facts.append(fact)
        if ok:
            verified.setdefault(c.key, {"label": label, "value": c.value, "unit": c.unit, "as_of": c.as_of, "basis": c.basis,
                                    "url": c.source_url})
        elif c.key not in verified:
            gaps.setdefault(c.key, f"a figure was found but could not be verified ({why})")
    store.add_facts(facts)
    for key in verified:
        gaps.pop(key, None)
    return {"verified": verified, "gaps": gaps}


def research_one(ctx: Context, task: str, *, label: str, group: str, quarter_end: date | None = None) -> tuple[Outcome, dict]:
    out = run_subagent(RESEARCHER, task, label=label, ctx=ctx)
    recorded = {"verified": {}, "gaps": {}}
    if out.status == "done" and out.result is not None:
        recorded = record_claims(ctx.store, out.result, group=group, quarter_end=quarter_end)
        from . import records

        records.attach_recorded(ctx.store, out.id, recorded)
    return out, recorded
