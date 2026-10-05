"""Render the stored report to a branded .docx, then .pdf.

Design is taken from the client's Brand Guide and template: navy #00223B and
tangerine #E25A00; Larken for display type and TT Norms for text and tables; a
full-bleed navy cover carrying the white logo over the brand's watermark.
Structure follows the delivered reports: cover, contents, numbered sections that
each start on a new page, tables with repeating navy headers, a centred footer.

Two gates run before a byte is written, and either blocks the render:
  * the numeric gate -- every figure in prose, tables and charts is in the ledger;
  * the lint -- the report never narrates its own data problems. Anything about
    the data belongs in a review note, not in the document.

A section appears only if there is something to say in it. Absence is data: a
section with no content is simply not rendered, and its absence is a review note.
"""

from __future__ import annotations

import os

import re
from pathlib import Path

from docx import Document
from docx.enum.section import WD_ORIENT, WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Emu, Mm, Pt, RGBColor

from . import assets
from .charts import render_chart
from .convert import assert_font_available, docx_to_pdf
from .gate import check_grounded, gate_report  # noqa: F401  (re-exported)
from .lint import lint_report

OUT_DIR = Path(os.environ.get("CHUI_OUT_DIR", "build_artifacts"))

# ---- brand tokens ---------------------------------------------------------
HEAD, HEAD_REG, HEAD_IT = "Larken-Bold", "Larken-Regular", "Larken-Italic"
# Larken ships as separate families per weight ("Larken-Bold", "Larken-Italic"), so bold and
# italic are chosen by family, never by synthesising. TT Norms (the brand's sans) cannot be
# loaded by LibreOffice on macOS -- its CFF/AAT build is rejected -- so text is set in Larken.
BODY, BODY_B = "Larken-Regular", "Larken-Bold"
NAVY, TANGERINE, AMBER = "00223B", "E25A00", "E28B00"
INK, MUTED, RULE = "24313D", "8C9AAA", "D7DDE3"
BAND, TOTAL_FILL, BANNER_FILL = "F5F7F9", "E9EEF2", "FDF1E8"

SECTION_TITLES = {
    "1": "Executive Summary", "2": "Fund Update & Overview", "3": "Macroeconomic Snapshot",
    "4": "Financial Statements", "5": "Portfolio Overview", "6": "Valuation Summary",
    "7": "Portfolio Company Performance",
}
PORTRAIT_TEXT_MM, LANDSCAPE_TEXT_MM = 170.0, 267.0


class UngroundedNumber(ValueError):
    """A figure that no fact in the ledger supports."""


class MetaCommentary(ValueError):
    """The report talks about its own data or production instead of reporting."""


# ---------------------------------------------------------------------------
# low-level OOXML helpers
# ---------------------------------------------------------------------------


def _rgb(hexstr: str) -> RGBColor:
    return RGBColor.from_string(hexstr)


def _set_fonts(rpr, name: str) -> None:
    rf = rpr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts")
        rpr.insert(0, rf)
    for a in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rf.set(qn(a), name)


def run(par, text, *, font=BODY, size=10, color=INK, bold=False, italic=False, track=None):
    if bold and font == BODY:
        font, bold = BODY_B, False
    if italic and font == BODY:
        font, italic = HEAD_IT, False
    r = par.add_run(text)
    r.font.size = Pt(size)
    r.font.color.rgb = _rgb(color)
    r.bold = bold
    r.italic = italic
    rpr = r._element.get_or_add_rPr()
    _set_fonts(rpr, font)
    if track:
        sp = OxmlElement("w:spacing")
        sp.set(qn("w:val"), str(int(track * 20)))
        rpr.append(sp)
    return r


def para(container, *, align=None, before=0, after=0, line=None, keep_next=False,
         page_break_before=False, border_bottom=None, border_top=None, keep_together=True):
    p = container.add_paragraph()
    pf = p.paragraph_format
    pf.space_before, pf.space_after = Pt(before), Pt(after)
    if line:
        pf.line_spacing = line
    pf.keep_with_next = keep_next
    pf.keep_together = keep_together
    pf.page_break_before = page_break_before
    if align:
        p.alignment = align
    for side, spec in (("bottom", border_bottom), ("top", border_top)):
        if spec:
            color, sz, space = spec
            ppr = p._p.get_or_add_pPr()
            bd = ppr.find(qn("w:pBdr")) or OxmlElement("w:pBdr")
            if bd.getparent() is None:
                ppr.append(bd)
            el = OxmlElement(f"w:{side}")
            el.set(qn("w:val"), "single")
            el.set(qn("w:sz"), str(sz))
            el.set(qn("w:space"), str(space))
            el.set(qn("w:color"), color)
            bd.append(el)
    return p


def _shade(cell, fill: str) -> None:
    tcpr = cell._tc.get_or_add_tcPr()
    for old in tcpr.findall(qn("w:shd")):
        tcpr.remove(old)
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tcpr.append(shd)


def _cell_borders(cell, **edges) -> None:
    tcpr = cell._tc.get_or_add_tcPr()
    b = tcpr.find(qn("w:tcBorders"))
    if b is None:
        b = OxmlElement("w:tcBorders")
        tcpr.append(b)
    for edge, spec in edges.items():
        el = OxmlElement(f"w:{edge}")
        if spec is None:
            el.set(qn("w:val"), "nil")
        else:
            color, sz = spec
            el.set(qn("w:val"), "single")
            el.set(qn("w:sz"), str(sz))
            el.set(qn("w:space"), "0")
            el.set(qn("w:color"), color)
        b.append(el)


def _cell_margins(table, top=30, bottom=30, left=80, right=80) -> None:
    tblpr = table._tbl.tblPr
    m = OxmlElement("w:tblCellMar")
    for side, v in (("top", top), ("left", left), ("bottom", bottom), ("right", right)):
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:w"), str(v))
        el.set(qn("w:type"), "dxa")
        m.append(el)
    tblpr.append(m)


def _fixed_layout(table) -> None:
    lay = OxmlElement("w:tblLayout")
    lay.set(qn("w:type"), "fixed")
    table._tbl.tblPr.append(lay)


def _row_flags(row, header=False) -> None:
    trpr = row._tr.get_or_add_trPr()
    cs = OxmlElement("w:cantSplit")
    trpr.append(cs)
    if header:
        th = OxmlElement("w:tblHeader")
        trpr.append(th)


def _field(par, instr: str, **kw) -> None:
    def part(kind):
        r = run(par, "", **kw)
        fc = OxmlElement("w:fldChar")
        fc.set(qn("w:fldCharType"), kind)
        r._r.append(fc)

    part("begin")
    r = run(par, "", **kw)
    it = OxmlElement("w:instrText")
    it.set(qn("xml:space"), "preserve")
    it.text = f" {instr} "
    r._r.append(it)
    part("separate")
    run(par, "1", **kw)
    part("end")


def _anchor_background(doc, par, image: Path, cx: Emu, cy: Emu) -> None:
    """Place `image` behind the text, pinned to the page's top-left corner."""
    r = par.add_run()
    r.add_picture(str(image), width=cx, height=cy)
    drawing = r._r.find(qn("w:drawing"))
    inline = drawing.find(qn("wp:inline"))
    graphic = inline.find(qn("a:graphic"))
    anchor = OxmlElement("wp:anchor")
    for k, v in dict(distT="0", distB="0", distL="0", distR="0", simplePos="0",
                     relativeHeight="0", behindDoc="1", locked="1", layoutInCell="1",
                     allowOverlap="1").items():
        anchor.set(k, v)
    sp = OxmlElement("wp:simplePos")
    sp.set("x", "0")
    sp.set("y", "0")
    anchor.append(sp)
    for tag in ("positionH", "positionV"):
        pos = OxmlElement(f"wp:{tag}")
        pos.set("relativeFrom", "page")
        off = OxmlElement("wp:posOffset")
        off.text = "0"
        pos.append(off)
        anchor.append(pos)
    ext = OxmlElement("wp:extent")
    ext.set("cx", str(int(cx)))
    ext.set("cy", str(int(cy)))
    anchor.append(ext)
    ee = OxmlElement("wp:effectExtent")
    for k in ("l", "t", "r", "b"):
        ee.set(k, "0")
    anchor.append(ee)
    anchor.append(OxmlElement("wp:wrapNone"))
    dp = OxmlElement("wp:docPr")
    dp.set("id", "900")
    dp.set("name", "Cover background")
    anchor.append(dp)
    anchor.append(OxmlElement("wp:cNvGraphicFramePr"))
    anchor.append(graphic)
    drawing.remove(inline)
    drawing.append(anchor)


def _page(section, landscape: bool) -> None:
    w, h = Mm(210), Mm(297)
    section.orientation = WD_ORIENT.LANDSCAPE if landscape else WD_ORIENT.PORTRAIT
    section.page_width, section.page_height = (h, w) if landscape else (w, h)
    section.left_margin = section.right_margin = Mm(15 if landscape else 20)
    section.top_margin, section.bottom_margin = (Mm(16), Mm(16)) if landscape else (Mm(20), Mm(20))
    section.footer_distance = Mm(9)


# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------

_INLINE = re.compile(r"(\*\*[^*]+\*\*|\*[^*]+\*)")


def rich(par, text: str, *, size=10, color=INK) -> None:
    """**bold** and *italic* inline."""
    for chunk in _INLINE.split(text):
        if not chunk:
            continue
        if chunk.startswith("**") and chunk.endswith("**"):
            run(par, chunk[2:-2], font=BODY_B, size=size, color=color)
        elif chunk.startswith("*") and chunk.endswith("*"):
            run(par, chunk[1:-1], font=BODY, size=size, color=color, italic=True)
        else:
            run(par, chunk, size=size, color=color)


def _numberish(text: str) -> bool:
    return bool(re.fullmatch(r"[\s$€£~(−-]*[\d][\d,.\s]*[)%xXkKMBT]*\s*", text.strip() or "x")) \
        or text.strip() in {"-", "–", "—"}


# ---------------------------------------------------------------------------
# document parts
# ---------------------------------------------------------------------------


def _cover(doc, meta: dict, bg: Path) -> None:
    sec = doc.sections[0]
    _page(sec, False)
    sec.left_margin = sec.right_margin = Mm(22)
    sec.top_margin, sec.bottom_margin = Mm(20), Mm(14)
    sec.footer.is_linked_to_previous = False   # blank footer on the cover

    first = doc.paragraphs[0] if doc.paragraphs else para(doc)
    _anchor_background(doc, first, bg, Mm(210), Mm(297))
    logo = para(doc, after=0)
    logo.add_run().add_picture(str(assets.logo_on_navy()), width=Mm(64))

    from .. import period as _pr

    P = _pr.current()
    quarter = meta.get("quarter", P.label)
    block = para(doc, before=262, after=6)
    run(block, f"{meta.get('fund', 'CHUI VENTURES FUND I').upper()}", font=BODY_B, size=10.5,
        color=TANGERINE, track=3)
    t = para(doc, after=2, line=0.92)
    run(t, meta.get("title", "Investor Quarterly Report"), font=HEAD, size=46, color="FFFFFF")
    q = para(doc, after=8)
    run(q, quarter, font=HEAD_IT, size=28, color="FFFFFF")
    pr = para(doc, after=22)
    run(pr, str(meta.get("period", P.range_label)).upper(), font=BODY, size=10.5,
        color=TANGERINE, track=3)

    rule = para(doc, before=8, after=8, border_top=(TANGERINE, 8, 6))
    run(rule, "CONFIDENTIAL", font=BODY_B, size=9, color=TANGERINE, track=2.5)
    note = para(doc, after=34)
    run(note, meta.get("disclaimer", "Prepared for Limited Partners only. Not for distribution."),
        size=9, color="A9B8C5", italic=True)

    fields = [("REPORTING DATE", meta.get("reporting_date", P.end_label)),
              ("FUND MANAGER", meta.get("manager", "Chui Ventures")),
              ("JURISDICTION", meta.get("jurisdiction", "Delaware / Mauritius")),
              ("DOCUMENT VERSION", meta.get("version", "v1.0"))]
    tbl = doc.add_table(rows=1, cols=4)
    tbl.alignment = WD_TABLE_ALIGNMENT.LEFT
    _fixed_layout(tbl)
    for i, (lab, val) in enumerate(fields):
        c = tbl.rows[0].cells[i]
        c.width = Mm(41)
        c.paragraphs[0].paragraph_format.space_after = Pt(2)
        run(c.paragraphs[0], lab, size=7, color="8FA3B5", track=1.6)
        p2 = c.add_paragraph()
        run(p2, val, font=BODY_B, size=9.5, color="FFFFFF")
        _cell_borders(c, top=None, bottom=None, left=None, right=None)


def _footer(section) -> None:
    section.footer.is_linked_to_previous = False
    p = section.footer.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(0)
    bd = OxmlElement("w:pBdr")
    top = OxmlElement("w:top")
    for k, v in (("val", "single"), ("sz", "4"), ("space", "6"), ("color", RULE)):
        top.set(qn(f"w:{k}"), v)
    bd.append(top)
    p._p.get_or_add_pPr().append(bd)
    kw = dict(size=7.5, color=MUTED)
    run(p, "Chui Ventures Fund I   |   For Limited Partners Only   |   Page ", **kw)
    _field(p, "PAGE", **kw)


def _toc_page(doc, entries: list[tuple[int, str]], pages: dict[str, int] | None) -> None:
    sec = doc.add_section(WD_SECTION.NEW_PAGE)
    _page(sec, False)
    _footer(sec)
    h = para(doc, after=14, border_bottom=(TANGERINE, 8, 6))
    run(h, "Contents", font=HEAD, size=24, color=NAVY)
    for level, text in entries:
        p = para(doc, before=7 if level == 1 else 1.5, after=1.5)
        p.paragraph_format.left_indent = Mm(0 if level == 1 else 6)
        p.paragraph_format.tab_stops.add_tab_stop(
            Mm(PORTRAIT_TEXT_MM), alignment=2, leader=1)   # right-aligned, dot leader
        run(p, text, font=BODY_B if level == 1 else BODY, size=10 if level == 1 else 9,
            color=NAVY if level == 1 else INK)
        num = (pages or {}).get(text)
        run(p, f"\t{num if num else ''}", font=BODY_B if level == 1 else BODY,
            size=10 if level == 1 else 9, color=NAVY if level == 1 else INK)


def _h1(doc, num: str, title: str, *, force_break: bool) -> None:
    kick = para(doc, before=0, after=2, keep_next=True, page_break_before=force_break)
    run(kick, f"SECTION {num}", font=BODY_B, size=8.5, color=TANGERINE, track=2.5)
    h = para(doc, after=12, keep_next=True, border_bottom=(RULE, 6, 6))
    run(h, title, font=HEAD, size=22, color=NAVY)


def _h2(doc, text: str, break_before: bool = False) -> None:
    p = para(doc, before=14, after=5, keep_next=True, page_break_before=break_before)
    run(p, text, font=HEAD, size=13, color=NAVY)


def _caption(doc, text: str, break_before: bool = False) -> None:
    p = para(doc, before=10, after=4, keep_next=True, page_break_before=break_before)
    run(p, text, font=HEAD, size=10.5, color=NAVY)


def _table_font(t: dict) -> float:
    n = len(t["columns"])
    return float((t.get("options") or {}).get("font_size",
                 9 if n <= 6 else 8.5 if n <= 8 else 8 if n <= 11 else 7.2))


def _row_mm(size: float) -> float:
    """Exact row pitch: line height is fixed at 1.22 x the font, plus cell padding."""
    return size * 1.22 * 0.3528 + 1.1 + 0.25


def _table(doc, t: dict, text_mm: float, section_title: str = "", break_before: bool = False) -> None:
    opts = t.get("options") or {}
    cols, rows = t["columns"], t["rows"]
    n = len(cols)
    size = _table_font(t)
    same = str(t.get("title") or "").strip().lower() == section_title.strip().lower()
    if t.get("title") and not same:        # a caption that repeats the heading is noise
        _caption(doc, str(t["title"]), break_before=break_before)
    elif break_before:
        para(doc, page_break_before=True)  # nothing to carry the break: a hairline spacer does

    # column widths: explicit weights, else sized by content
    weights = opts.get("widths")
    if not weights:
        weights = []
        for c in range(n):
            longest = max([len(str(cols[c]))] + [len(str(r[c])) for r in rows if c < len(r)])
            weights.append(min(max(longest, 6), 34) + (6 if c == 0 else 0))
    total = float(sum(weights))
    widths = [Mm(text_mm * w / total) for w in weights]

    tbl = doc.add_table(rows=1, cols=n)
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    _fixed_layout(tbl)
    _cell_margins(tbl)
    def numeric_col(c: int) -> bool:
        body = [str(r[c]) for r in rows if c < len(r) and r[c] not in (None, "")]
        return bool(body) and sum(_numberish(x) for x in body) >= 0.7 * len(body)

    # header and cells must agree, so alignment is decided once per column
    aligns = opts.get("align") or [("l" if c == 0 else ("r" if numeric_col(c) else "l"))
                                   for c in range(n)]

    for i, name in enumerate(cols):
        cell = tbl.rows[0].cells[i]
        cell.width = widths[i]
        _shade(cell, NAVY)
        _cell_borders(cell, top=None, left=None, right=None, bottom=(TANGERINE, 12))
        p = cell.paragraphs[0]
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.line_spacing = Pt(size * 1.22)
        p.alignment = {"r": WD_ALIGN_PARAGRAPH.RIGHT, "c": WD_ALIGN_PARAGRAPH.CENTER}.get(
            aligns[i], WD_ALIGN_PARAGRAPH.LEFT)
        run(p, str(name), font=BODY_B, size=size - 0.3, color="FFFFFF")
    _row_flags(tbl.rows[0], header=True)

    totals = {(k if k >= 0 else len(rows) + k) for k in opts.get("total_rows", [])}
    banners = set(opts.get("banner_rows", []))
    band = 0
    for ri, row in enumerate(rows):
        cells = tbl.add_row().cells
        _row_flags(tbl.rows[-1])
        for i in range(n):
            cells[i].width = widths[i]
        if ri in banners:
            m = cells[0].merge(cells[-1])
            _shade(m, BANNER_FILL)
            _cell_borders(m, top=(TANGERINE, 6), bottom=(RULE, 4), left=None, right=None)
            m.paragraphs[0].paragraph_format.space_after = Pt(0)
            m.paragraphs[0].paragraph_format.line_spacing = Pt(size * 1.22)
            run(m.paragraphs[0], str(row[0]).upper(), font=BODY_B, size=size - 0.5,
                color=TANGERINE, track=1.2)
            band = 0
            continue
        is_total = ri in totals
        for i in range(n):
            val = row[i] if i < len(row) else ""
            text = "" if val is None else str(val)
            c = cells[i]
            p = c.paragraphs[0]
            p.paragraph_format.space_after = Pt(0)
            p.paragraph_format.line_spacing = Pt(size * 1.22)
            p.alignment = {"r": WD_ALIGN_PARAGRAPH.RIGHT, "c": WD_ALIGN_PARAGRAPH.CENTER}.get(
                aligns[i], WD_ALIGN_PARAGRAPH.LEFT)
            run(p, text, font=BODY_B if is_total else BODY, size=size, color=NAVY if is_total else INK)
            if is_total:
                _shade(c, TOTAL_FILL)
                _cell_borders(c, top=(NAVY, 8), bottom=(NAVY, 8), left=None, right=None)
            else:
                if band % 2 == 1:
                    _shade(c, BAND)
                _cell_borders(c, top=None, left=None, right=None, bottom=(RULE, 4))
        band += 1

    if opts.get("note"):
        n_ = para(doc, before=3, after=8)
        run(n_, str(opts["note"]), size=7.5, color=MUTED, italic=True)
    else:
        para(doc, after=6)


def _chart(doc, key: str, c: dict, text_mm: float, workdir: Path, break_before: bool = False) -> None:
    o = c.get("options") or {}
    img = render_chart(c["kind"], list(c["labels"]), [float(v) for v in c["values"]],
                       workdir / f"chart_{key}.png", value_format=o.get("value_format", "{:,.0f}"),
                       show_values=o.get("show_values", True),
                       width_in=text_mm / 25.4, height_in=float(o.get("height_in", 3.0)))
    if c.get("title"):
        _caption(doc, str(c["title"]), break_before=break_before)
        p = para(doc, align=WD_ALIGN_PARAGRAPH.CENTER, after=8)
    else:
        p = para(doc, align=WD_ALIGN_PARAGRAPH.CENTER, after=8, page_break_before=break_before)
    p.add_run().add_picture(str(img), width=Mm(text_mm))


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------


PAGE_PORTRAIT_MM, PAGE_LANDSCAPE_MM = 244.0, 158.0     # usable height after margins and footer


def _est_par(text: str, text_mm: float) -> float:
    per_line = 94 if text_mm < 200 else 158                      # characters per line at 10pt
    return max(1, -(-len(text) // per_line)) * 5.3 + 3.2


def _est_table(t: dict) -> float:
    size = _table_font(t)
    cap = 0 if str(t.get("title") or "").strip() == "" else 9
    header = 2 * _row_mm(size) if len(t["columns"]) > 5 else _row_mm(size)   # headers often wrap
    return cap + header + len(t["rows"]) * _row_mm(size) * 1.03 + 8


def _est_chart(c: dict) -> float:
    return 10 + float((c.get("options") or {}).get("height_in", 3.0)) * 25.4 + 10


def _key(k: str):
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", k))


def build_document(sections: list[dict], tables: dict[str, dict], charts: dict[str, dict],
                   meta: dict, dest: Path, pages: dict[str, int] | None = None) -> list[tuple[int, str]]:
    """Build the .docx. Returns the contents entries, in order."""
    workdir = dest.parent
    doc = Document()
    st = doc.styles["Normal"]
    st.font.name, st.font.size = BODY, Pt(10)
    _set_fonts(st.element.get_or_add_rPr(), BODY)

    secs = sorted([s for s in sections if s.get("present", True)], key=lambda s: _key(s["key"]))
    by_sec_t: dict[str, list] = {}
    by_sec_c: dict[str, list] = {}
    for k, t in sorted(tables.items()):
        by_sec_t.setdefault(t.get("section_key") or "", []).append((k, t))
    for k, c in sorted(charts.items()):
        by_sec_c.setdefault(c.get("section_key") or "", []).append((k, c))
    secs = [s for s in secs if s["body"].strip() or s["key"] in by_sec_t or s["key"] in by_sec_c]

    # Numbering follows what is actually in the report. Omitting a section (say, macro)
    # must not leave "Section 2" followed by "Section 4": majors and minors are renumbered
    # consecutively over the sections present.
    majors: list[str] = []
    minors: dict[str, list[str]] = {}
    for s in secs:
        maj = s["key"].split(".")[0]
        if maj not in majors:
            majors.append(maj)
        minors.setdefault(maj, []).append(s["key"])
    renum = {m: str(i + 1) for i, m in enumerate(majors)}

    def disp(key: str) -> str:
        maj = key.split(".")[0]
        return f"{renum[maj]}.{minors[maj].index(key) + 1}"

    entries: list[tuple[int, str]] = []
    seen_major: set[str] = set()
    for s in secs:
        major = s["key"].split(".")[0]
        if major not in seen_major:
            seen_major.add(major)
            entries.append((1, f"Section {renum[major]} — {SECTION_TITLES.get(major, 'Report')}"))
        entries.append((2, f"{disp(s['key'])} {s['title']}"))

    bg = assets.cover_background(workdir / "cover_bg.png")
    _cover(doc, meta, bg)
    _toc_page(doc, entries, pages)

    seen_major.clear()
    landscape_now = False
    page_h = PAGE_PORTRAIT_MM
    used = 0.0                 # estimated height already used on the current page
    at_page_start = False      # the contents page precedes the first section
    for s in secs:
        major = s["key"].split(".")[0]
        tabs, chs = by_sec_t.get(s["key"], []), by_sec_c.get(s["key"], [])
        wants_land = any((t.get("options") or {}).get("landscape") for _, t in tabs)
        if wants_land != landscape_now:
            sec = doc.add_section(WD_SECTION.NEW_PAGE)
            _page(sec, wants_land)
            landscape_now, at_page_start, used = wants_land, True, 0.0
            page_h = PAGE_LANDSCAPE_MM if wants_land else PAGE_PORTRAIT_MM
        text_mm = LANDSCAPE_TEXT_MM if landscape_now else PORTRAIT_TEXT_MM
        if major not in seen_major:
            seen_major.add(major)
            _h1(doc, renum[major], SECTION_TITLES.get(major, "Report"), force_break=not at_page_start)
            used = 24.0
        at_page_start = False

        paras = [c for c in re.split(r"\n\s*\n|\n", s["body"]) if c.strip()]
        first_block = (_est_par(paras[0], text_mm) if paras else
                       min(_est_table(tabs[0][1]), page_h * 0.6) if tabs else
                       min(_est_chart(chs[0][1]), page_h * 0.6) if chs else 0.0)
        need = 12.0 + first_block
        brk = used > 0 and need <= page_h and used + need > page_h
        _h2(doc, f"{disp(s['key'])} {s['title']}", break_before=brk)
        used = (12.0 if brk else used + 12.0)

        for chunk in paras:
            if chunk.lstrip().startswith(("- ", "• ")):
                p = para(doc, after=3)
                p.paragraph_format.left_indent = Mm(5)
                p.paragraph_format.first_line_indent = Mm(-3.5)
                run(p, "•  ", color=TANGERINE, font=BODY_B)
                rich(p, chunk.lstrip()[2:].strip())
            else:
                p = para(doc, after=6, line=1.32)
                rich(p, chunk.strip())
            used += _est_par(chunk, text_mm)
            if used > page_h:
                used %= page_h
        blocks = [("t", k, t) for k, t in tabs] + [("c", k, c) for k, c in chs]
        for kind, k, obj in blocks:
            h = _est_table(obj) if kind == "t" else _est_chart(obj)
            brk = h <= page_h and used + h > page_h and used > 0
            if kind == "t":
                _table(doc, obj, text_mm, s["title"], break_before=brk)
            else:
                _chart(doc, k, obj, text_mm, workdir, break_before=brk)
            used = h if brk else used + h
            if used > page_h:
                used %= page_h

    # first body section carries the footer for everything after it
    doc.sections[1].footer.is_linked_to_previous = False
    for sec in doc.sections[2:]:
        sec.footer.is_linked_to_previous = True
    doc.core_properties.title = f"{meta.get('fund', 'Chui Ventures Fund I')} — {meta.get('quarter', '')}"
    doc.core_properties.author = "Chui Ventures"
    doc.save(str(dest))
    return entries


def heading_pages(pdf: Path, entries: list[tuple[int, str]]) -> dict[str, int]:
    """Page on which each contents entry actually lands, read back from the PDF."""
    from pypdf import PdfReader

    reader = PdfReader(str(pdf))
    texts = [" ".join((p.extract_text() or "").split()) for p in reader.pages]
    out: dict[str, int] = {}
    for _, label in entries:
        needle = " ".join(label.replace("Section ", "").split(" — ")[-1].split()) \
            if label.startswith("Section ") else " ".join(label.split())
        for i, t in enumerate(texts):
            if i < 2:                    # cover and contents
                continue
            if needle in t:
                out[label] = i + 1
                break
    return out


def check_report(store) -> tuple[list[dict], dict, dict, dict]:
    """Both gates. Raises UngroundedNumber or MetaCommentary, naming every offender."""
    sections, tables, charts, meta = store.sections(), store.tables(), store.charts(), store.meta()
    # a table or chart must belong to a section that will be rendered
    live = {s["key"] for s in sections}
    orphans = [f"{k} (section {v.get('section_key')!r})" for k, v in {**tables, **charts}.items()
               if v.get("section_key") not in live]
    if orphans:
        raise ValueError("these tables/charts are not attached to a present section: "
                         + ", ".join(orphans))
    chart_tables = {f"chart:{k}": {"columns": ["label", "value"],
                                   "rows": [[l, v] for l, v in zip(c["labels"], c["values"])]}
                    for k, c in charts.items()}
    offences = gate_report(sections, {**tables, **chart_tables}, store.grounded_values())
    violations = lint_report(sections, tables, charts, meta)
    if offences:
        raise UngroundedNumber(
            "these figures are not supported by any fact in the ledger:\n  " + "\n  ".join(offences)
            + "\nGround them (domain tools, report_derive_fact, verified report_save_facts) or remove them."
            + (("\n\nAlso, meta-commentary:\n  " + "\n  ".join(map(str, violations))) if violations else ""))
    if violations:
        raise MetaCommentary(
            "the report must not discuss its own data, sources or production. Move each of these "
            "to report_review_note and replace it with finished prose, or omit the passage/section:\n  "
            + "\n  ".join(map(str, violations)))
    return sections, tables, charts, meta


def render_report(store, file_stem: str) -> tuple[Path, Path, dict]:
    from .pdfium_safe import RENDER_LOCK

    with RENDER_LOCK:                       # parallel tool calls must not write the same files at once
        return _render_report(store, file_stem)


def _render_report(store, file_stem: str) -> tuple[Path, Path, dict]:
    assert_font_available("Larken")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sections, tables, charts, meta = check_report(store)
    if not sections:
        raise ValueError("report is empty -- add sections before rendering")

    docx_path = OUT_DIR / f"{file_stem}.docx"
    entries = build_document(sections, tables, charts, meta, docx_path)
    pdf_path = docx_to_pdf(docx_path, OUT_DIR)
    pages = heading_pages(pdf_path, entries)            # second pass: real page numbers
    build_document(sections, tables, charts, meta, docx_path, pages)
    pdf_path = docx_to_pdf(docx_path, OUT_DIR)
    from .notes import write_review_notes

    notes_path = write_review_notes(store, OUT_DIR / f"{file_stem} - review notes.md")
    return docx_path, pdf_path, {"sections": len(sections), "tables": len(tables),
                                 "charts": len(charts), "review_notes": str(notes_path),
                                 "heading_pages": pages}
