"""How a number becomes allowed in the report.

The numeric gate trusts the fact ledger, so the ledger must not be something the
agent can write whatever it likes into. Four ways in, and only these:

1. RECORDED by code from a deterministic read. Provenance comes from the
   extractor, never from the model.
2. VERIFIED: the agent asserts a fact and cites a cell or page. The cited source
   is re-read and the number must really be there. If it is not, or cannot be
   checked, the fact is stored as `claimed` and licenses nothing.
3. DERIVED: computed by code from other grounded facts. The agent names the
   inputs and the arithmetic; it never types the result.
4. PROVIDED: the user's own word is a source. A figure they typed in the conversation is accepted when the
   agent quotes their message (and the quote really is in what they wrote); a figure the agent read from
   an image they attached is accepted as theirs, and flagged for them to check, because nothing can verify it by
   machine. The person who owns the data being unable to give it to the agent would make the agent useless
   exactly when it is stuck.

Without this, the gate only proves "the agent wrote this number down somewhere",
and an invented figure could be grounded by saving it with an invented source.
"""

from __future__ import annotations

import ast
import io
import operator
import re
from pathlib import Path

from openpyxl.utils.cell import coordinate_from_string, column_index_from_string

from ..extract.images import IMAGE_EXT
from ..extract.workbook import Value, Workbook
from .store import Fact, Store

# A cell holding 0.9227 under a "USD millions" header is stated as 922,727 in the
# report, so a verified fact may differ from its cell by a declared unit scale.
_SCALES = (1.0, 1e3, 1e6, 100.0, 0.01)


# --------------------------------------------------------------------------
# 1. recording from deterministic reads
# --------------------------------------------------------------------------


def fact_from_value(label: str, v: Value, *, usd: bool = True, note: str | None = None,
                    as_of: str | None = None) -> Fact | None:
    """A ledger entry from an extracted Value. Quarantined values are recorded
    as quarantined -- visibly -- and never license a figure."""
    prov = v.provenance
    if not v.is_usable:
        return Fact(label, None, unit="USD", source_file=Path(prov.file_path).name,
                    source_sheet=prov.sheet, source_cell=prov.cell, status="quarantined",
                    note=v.note or "unusable source value")
    num = v.as_usd() if usd and v.unit.startswith("USD") else v.raw
    if not isinstance(num, (int, float)) or isinstance(num, bool):
        return None
    return Fact(label, float(num), unit="USD" if usd else v.unit,
                source_file=Path(prov.file_path).name, source_sheet=prov.sheet,
                source_cell=prov.cell, as_of=as_of, status="extracted",
                note=note or (f"cell unit {v.unit}" if v.unit != "USD" else None))


def derived(label: str, value: float, inputs: list[str], formula: str,
            unit: str = "USD") -> Fact:
    return Fact(label, float(value), unit=unit, source_file="derived",
                status="derived", note=f"{formula}  [inputs: {'; '.join(inputs)}]")


# --------------------------------------------------------------------------
# 2. verification of agent-asserted facts
# --------------------------------------------------------------------------


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.005, abs(b) * 1e-9)


def verify_claim(f: Fact, resolve, store: Store | None = None) -> tuple[bool, str]:
    """Does the cited source really contain this number?"""
    ok, why, _ = verify_claim_ex(f, resolve, store)
    return ok, why


_IMAGES = IMAGE_EXT


def verify_claim_ex(f: Fact, resolve, store: Store | None = None) -> tuple[bool, str, str]:
    """(ok, why, status). The status a verified fact earns is 'extracted' when code found the figure in the source, and
    'provided' when the user's own word is the source (typed by them, or read from an image they attached)."""
    if f.value is None:
        return False, "no numeric value", "claimed"
    if not f.source_file:
        return False, "no source_file cited", "claimed"
    if f.source_file.strip().lower().startswith("user:"):
        ok, why = verify_user_claim(f, store)
        return ok, why, "provided"
    if f.source_file.lower().startswith(("http://", "https://")):
        from ..services.web import verify_web_claim

        ok, why = verify_web_claim(f, store)
        return ok, why, "extracted"
    try:
        path: Path = resolve(f.source_file)
    except FileNotFoundError:
        return False, f"cited file {f.source_file!r} not found", "claimed"

    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        return (*_verify_cell(f, path), "extracted")
    if suffix == ".pdf":
        return (*_verify_pdf(f, path), "extracted")
    if suffix in {".csv", ".txt", ".md", ".json"}:
        text = path.read_text(encoding="utf-8", errors="ignore")[:2_000_000]
        if suffix == ".csv":        # cells, not raw lines: a comma between cells must not look like a thousands separator
            import csv

            text = "\n".join(" | ".join(row) for row in csv.reader(io.StringIO(text)))
        return (*_verify_text(f, text, path.name), "extracted")
    if suffix == ".docx":
        return (*_verify_docx(f, path), "extracted")
    if suffix in _IMAGES:
        if "Uploads" in path.parts:
            return (True, f"read from the image {path.name}, which you attached: it cannot be checked by machine, so it is "
                          f"recorded as yours and flagged for your review", "provided")
        return False, f"{path.name} is not an image the user attached in the conversation", "claimed"
    return False, f"cannot verify against a {suffix or 'unknown'} file", "claimed"


def _user_texts(store: Store | None) -> list[str]:
    """What the user wrote in this quarter's conversations: messages, and answers to the agent's questions."""
    if store is None:
        return []
    period = (store.report_id or "").removeprefix("fund-i-")
    with store.conn() as c:
        rows = list(c.execute(
            f"SELECT label, detail FROM {store._t('event')} WHERE kind IN ('steer','question.answered') "
            f"AND (session_id IS NULL OR session_id IN (SELECT id FROM {store._t('session')} WHERE period=%s)) "
            f"ORDER BY id DESC LIMIT 400", (period,)))
    return [str((r["detail"] or {}).get("text") or r["label"] or "") for r in rows]


def verify_user_claim(f: Fact, store: Store | None) -> tuple[bool, str]:
    """A figure the user gave the agent in words. The agent must quote the user (source_cell); the quote must really be in
    something they wrote, and must contain the figure."""
    from ..services.web import norm

    quote = (f.source_cell or "").strip()
    if len(quote) < 6:
        return False, "quote the user's own words in source_cell (the sentence that gives the figure)"
    q = norm(quote)
    if not any(q in norm(t) for t in _user_texts(store)):
        return False, "that quotation is not in anything the user wrote in this quarter's conversation; quote their words exactly"
    if not any(re.search(rf"(?<![\d.,]){re.escape(form)}(?![\d])", quote) for form in _pdf_forms(float(f.value))):
        return False, f"the quotation does not contain {f.value!r}"
    return True, f"stated by the user: “{quote[:140]}”"


def _verify_text(f: Fact, text: str, name: str) -> tuple[bool, str]:
    for form in _pdf_forms(float(f.value)):
        if re.search(rf"(?<![\d.,]){re.escape(form)}(?![\d])", text):
            return True, f"found {form!r} in {name}"
    return False, f"{f.value!r} not found in {name}"


def _verify_docx(f: Fact, path: Path) -> tuple[bool, str]:
    try:
        from docx import Document

        doc = Document(str(path))
        parts = [p.text for p in doc.paragraphs]
        for t in doc.tables:
            parts += [c.text for row in t.rows for c in row.cells]
    except Exception as exc:  # noqa: BLE001
        return False, f"could not read {path.name}: {exc}"
    return _verify_text(f, "\n".join(parts), path.name)


def _verify_cell(f: Fact, path: Path) -> tuple[bool, str]:
    if not (f.source_sheet and f.source_cell):
        return False, "workbook cited without sheet and cell; cite both"
    try:
        col_letters, row = coordinate_from_string(f.source_cell.upper())
        wb = Workbook.open(path)
        try:
            cell = wb.sheet(f.source_sheet).raw(row, column_index_from_string(col_letters))
        finally:
            wb.close()
    except Exception as exc:  # noqa: BLE001 - any failure means "not verified"
        return False, f"could not read {f.source_sheet}!{f.source_cell}: {exc}"
    if isinstance(cell, str):
        # Valuation workbooks carry narrative ("generated $787.5K in Q2 2026 revenue"):
        # a figure quoted in a text cell is verified by finding it written there.
        for form in _pdf_forms(float(f.value)):
            if re.search(rf"(?<![\d.,]){re.escape(form)}(?![\d])", cell):
                return True, f"found {form!r} in the text of {f.source_sheet}!{f.source_cell}"
        return False, f"{f.value!r} does not appear in the text of {f.source_sheet}!{f.source_cell}"
    if not isinstance(cell, (int, float)) or isinstance(cell, bool):
        return False, f"{f.source_sheet}!{f.source_cell} holds {cell!r}, not a number"
    for scale in _SCALES:
        if _close(abs(float(f.value)), abs(float(cell)) * scale):
            return True, f"matches {f.source_sheet}!{f.source_cell} = {cell!r} (x{scale:g})"
    return False, (f"{f.source_sheet}!{f.source_cell} holds {cell!r}; "
                   f"{f.value!r} is not that value or a unit-scaling of it")


def _pdf_forms(v: float) -> list[str]:
    a = abs(v)
    forms = {f"{a:,.2f}", f"{a:,.0f}", f"{a:.2f}", f"{a:.0f}", f"{a:,.1f}"}
    forms |= {f"{a / 1e3:,.0f}", f"{a / 1e3:,.1f}", f"{a / 1e6:.2f}", f"{a / 1e6:.1f}"}
    forms |= {f"{a * 100:.2f}", f"{a * 100:.1f}", f"{a * 100:.0f}"}
    forms |= {f"{a / 1e9:.2f}", f"{a / 1e9:.1f}"}
    # Many sources (BCEAO, INSEE, most of continental Europe and francophone Africa) write 0,8 for 0.8.
    forms |= {f.replace(".", ",") for f in forms if "." in f and "," not in f}
    return [x for x in forms if x not in {"0", "0.0", "0.00", "0,0", "0,00"}]


def _verify_pdf(f: Fact, path: Path) -> tuple[bool, str]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = range(len(reader.pages))
    if f.source_sheet:
        m = re.search(r"\d+", f.source_sheet)
        if m and 1 <= int(m.group()) <= len(reader.pages):
            pages = [int(m.group()) - 1]
    for i in pages:
        text = reader.pages[i].extract_text(extraction_mode="layout") or ""
        for form in _pdf_forms(float(f.value)):
            if re.search(rf"(?<![\d.,]){re.escape(form)}(?![\d])", text):
                return True, f"found {form!r} on page {i + 1}"
    return False, f"{f.value!r} not found in the cited page(s) of {path.name}"


# --------------------------------------------------------------------------
# 3. derived facts
# --------------------------------------------------------------------------

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv}


class DerivationError(ValueError):
    pass


def evaluate(expression: str, env: dict[str, float]) -> float:
    """Arithmetic over named inputs only: + - * / ( ) and numbers. No calls, no
    attributes, no names beyond the supplied inputs."""

    def go(n):
        if isinstance(n, ast.Expression):
            return go(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) \
                and not isinstance(n.value, bool):
            return float(n.value)
        if isinstance(n, ast.Name):
            if n.id not in env:
                raise DerivationError(f"unknown input {n.id!r}; declared: {sorted(env)}")
            return env[n.id]
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            right = go(n.right)
            if isinstance(n.op, ast.Div) and right == 0:
                raise DerivationError("division by zero")
            return _OPS[type(n.op)](go(n.left), right)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            return -go(n.operand) if isinstance(n.op, ast.USub) else go(n.operand)
        raise DerivationError(f"not allowed in a derivation: {ast.dump(n)[:60]}")

    try:
        return go(ast.parse(expression, mode="eval"))
    except SyntaxError as exc:
        raise DerivationError(f"invalid expression: {exc}") from exc


def derive_fact(store: Store, label: str, expression: str, inputs: dict[str, str],
                unit: str = "USD") -> Fact:
    """Compute `expression` over ledger facts. `inputs` maps a short name used in
    the expression to the label of a GROUNDED ledger fact; an input that is not
    grounded (missing, claimed, quarantined) refuses the derivation."""
    values = store.fact_values(list(inputs.values()))
    missing = [lab for lab in inputs.values() if lab not in values]
    if missing:
        raise DerivationError(
            f"these inputs are not grounded facts in the ledger: {missing}. A claimed or "
            f"quarantined fact cannot be an input.")
    env = {name: values[lab] for name, lab in inputs.items()}
    result = evaluate(expression, env)
    return derived(label, result, list(inputs.values()), expression, unit)
