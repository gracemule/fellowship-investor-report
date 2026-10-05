"""Snapshots of the rendered report.

Every time the report is rendered with different content it becomes a numbered version,
stored whole (PDF, Word, review notes), so the interface can show what changed and a bad
update can always be compared with the one before it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..agent.store import Store
from . import state


def _section_hashes(store: Store) -> dict[str, str]:
    sections, tables, charts = store.sections(), store.tables(), store.charts()
    out = {}
    for s in sections:
        parts = [s["title"], s["body"] or ""]
        for coll in (tables, charts):
            for k, v in sorted(coll.items()):
                if v.get("section_key") == s["key"]:
                    parts.append(k + json.dumps(v.get("rows") or v.get("values"), sort_keys=True, default=str))
        out[s["key"]] = hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:16]
    meta = {k: v for k, v in store.meta().items() if k not in ("rendered_at", "inspected_at", "last_render")}
    out["cover"] = hashlib.sha1(json.dumps(meta, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return out


def section_pages(pdf: Path, sections: list[dict]) -> dict[str, int]:
    """First page on which each section's title appears, in reading order, after the contents."""
    from pypdf import PdfReader

    texts = [" ".join((p.extract_text() or "").split()).casefold() for p in PdfReader(str(pdf)).pages]
    out, floor = {}, 2
    for s in sorted(sections, key=lambda r: (r["ord"], r["key"])):
        needle = " ".join(str(s["title"]).split()).casefold()
        for i in range(floor, len(texts)):
            if needle and needle in texts[i]:
                out[s["key"]] = i + 1
                floor = i
                break
    return out


def snapshot(store: Store, run_id: str | None = None) -> dict | None:
    """Store the latest render as a new version if its content differs from the previous one.
    Returns {version, changed, pages, new} or None when nothing has been rendered."""
    lr = (store.meta() or {}).get("last_render")
    if not lr or not Path(lr["pdf"]).exists():
        return None
    from pypdf import PdfReader

    pdf_path = Path(lr["pdf"])
    sections = store.sections()
    hashes = _section_hashes(store)
    prev = state.latest_version(store)
    prev_hashes = (prev or {}).get("summary", {}).get("hashes", {})
    changed = sorted([k for k, h in hashes.items() if prev_hashes.get(k) != h]
                     + [k for k in prev_hashes if k not in hashes])
    if prev and not changed:
        return {"version": prev["version"], "changed": [], "pages": prev["pages"], "new": False}
    pages = len(PdfReader(str(pdf_path)).pages)
    docx = Path(lr["docx"])
    notes = Path(lr["notes"]) if lr.get("notes") else None
    sp = section_pages(pdf_path, sections)
    from ..render.pdfium_safe import page_sizes

    pdf_bytes = pdf_path.read_bytes()
    summary = {"hashes": hashes, "changed": changed, "section_pages": sp, "headings": lr.get("headings", {}),
               "sizes": page_sizes(pdf_bytes),
               "sections": [{"key": s["key"], "title": s["title"]} for s in sections], "run_id": run_id}
    v = state.save_version(store, pages, summary, pdf_bytes,
                           docx.read_bytes() if docx.exists() else None,
                           notes.read_text() if notes and notes.exists() else None)
    return {"version": v, "changed": changed, "pages": pages, "new": True}
