"""The review notes, written as their own document.

These are everything about the DATA that a reviewer needs and an investor must not see:
gaps, source disagreements, stale marks, decisions required. They are produced beside the
report and never inside it.
"""

from __future__ import annotations

from pathlib import Path

_ORDER = [("decision", "Decisions needed"), ("warning", "Warnings"), ("info", "For information")]


def write_review_notes(store, path: Path) -> Path:
    notes = store.review_notes()
    label = (getattr(store, "meta", lambda: {})() or {}).get("quarter", "")
    out = [f"# Review notes — {label} Investor Quarterly Report".replace("—  ", "— "),
           "",
           "For the reviewing team. **Not part of the report** and not for distribution to LPs.",
           ""]
    if not notes:
        out.append("No notes were recorded.")
    for sev, heading in _ORDER:
        group = [n for n in notes if n["severity"] == sev]
        if not group:
            continue
        out += [f"## {heading} ({len(group)})", ""]
        for n in group:
            out += [f"### {n['area']}", "", n["text"].strip(), ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out), encoding="utf-8")
    return path
