"""Keeps the report a report.

A quarterly LP report is the fund manager's finished statement to its investors.
It never narrates how it was produced, what data was missing, which sources
disagree, or what was derived. All of that is for the reviewing human and goes to
`review_note`, never into the document.

The first end-to-end run produced a "report" full of exactly that: sections
saying a figure "cannot be completed from the source documents", two valuation
bases presented side by side "rather than selecting", a statement that the
workpapers "carry no fund-level metrics". Correct as observations, and
unacceptable as a deliverable. So this is enforced in code, not left to a prompt:
the render is blocked and the offending text is returned for the agent to move
into a review note and replace with finished prose, or to omit.

Deliberately narrow. It targets talk about the data, the process and the agent --
not ordinary financial wording such as "held at cost" or "carried at fair value".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PATTERNS: list[tuple[str, str]] = [
    (r"\bsource (set|documents?|workbooks?|files?|data|material)\b", "refers to the source data"),
    (r"\b(the )?(work ?papers?|working papers?)\b", "refers to the workpapers"),
    (r"\b(fact |the )?ledger\b", "refers to the ledger"),
    (r"\b(the )?(agent|tool|tooling|pipeline|extractor|backend|database)\b", "refers to the tooling"),
    (r"\b(cannot|can ?not|could not|unable to|not able to) be (completed|presented|determined|provided|reported|populated|calculated|derived)\b", "says something cannot be done"),
    (r"\b(is|are|was|were|remain|remains) (currently |still )?(missing|unavailable|not available|not provided|not supplied|outstanding|pending|incomplete)\b", "says data is missing"),
    (r"\bnot (available|provided|supplied|included|present|in the)\b", "says data is not available"),
    (r"\b(missing from|absent from|no data|no information)\b", "says data is missing"),
    (r"\bunavailable\b", "says something is unavailable"),
    (r"\bno [a-z ]{1,30} (is|are|was|were) (available|supplied|provided|included)\b", "says data is not available"),
    (r"\b(folder|file|sheet|tab|schedule) (is|are|was) (empty|blank)\b", "talks about an empty source"),
    (r"\bplaceholders?\b|\bto be (confirmed|populated|provided|completed|updated)\b|\bTBC\b|\bTBD\b|\bn/?a\b", "placeholder"),
    (r"\b(should|would|must) (have )?(come|be sourced|be obtained) from\b", "says where data should have come from"),
    (r"\brather than (selecting|resolving|choosing|picking|silently)\b", "explains a judgement about sources"),
    (r"\b(two|both|differing|competing) (bases|sources|sets of)\b|\bthe two sources\b|\bsources (disagree|differ|conflict)\b", "discusses a source disagreement"),
    (r"\b(derived|reconstructed|back-?solved|extracted) (from|using|by)\b", "explains how a figure was produced"),
    (r"\bwe (say|state|label|flag|note) (so|this|it)\b|\blabell?ed (as|accordingly)\b", "talks about its own labelling"),
    (r"\b(for (your )?review|reviewer|pending review|please (note|see|confirm|review))\b", "addressed to a reviewer"),
    (r"\b(as an ai|i (cannot|could not|will not)|i have|i will|let me|here is|here are|below is the)\b", "agent voice"),
    (r"\bDelaware'?s? (books|accounts|records|statements) (are|is) (not|missing)\b", "says Delaware's records are missing"),
    (r"\b(not|no longer) (be )?(fabricat|invent|supply|supplied from memory)\w*", "talks about not inventing figures"),
]
_COMPILED = [(re.compile(p, re.I), why) for p, why in _PATTERNS]


@dataclass(frozen=True)
class Violation:
    where: str
    phrase: str
    why: str
    context: str

    def __str__(self) -> str:
        return f"{self.where}: \"{self.phrase}\" ({self.why}) in \"...{self.context}...\""


def lint_text(text: str, where: str) -> list[Violation]:
    out: list[Violation] = []
    for rx, why in _COMPILED:
        for m in rx.finditer(text or ""):
            a, b = max(0, m.start() - 40), min(len(text), m.end() + 40)
            out.append(Violation(where, m.group(0), why, " ".join(text[a:b].split())))
    return out


def lint_report(sections: list[dict], tables: dict[str, dict],
                charts: dict[str, dict] | None = None, meta: dict | None = None) -> list[Violation]:
    out: list[Violation] = []
    for s in sections:
        out += lint_text(s["title"], f"section {s['key']} title")
        out += lint_text(s["body"], f"section {s['key']}")
    for key, t in sorted(tables.items()):
        out += lint_text(str(t.get("title") or ""), f"table {key} title")
        for r, row in enumerate(t["rows"], start=1):
            for cell in row:
                if isinstance(cell, str):
                    out += lint_text(cell, f"table {key} row {r}")
        out += lint_text(" ".join(map(str, t["columns"])), f"table {key} header")
    for key, c in sorted((charts or {}).items()):
        out += lint_text(str(c.get("title") or ""), f"chart {key} title")
    for k, v in (meta or {}).items():
        if isinstance(v, str):
            out += lint_text(v, f"cover {k}")
    return out
