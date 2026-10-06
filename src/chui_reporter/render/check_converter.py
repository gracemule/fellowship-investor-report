"""Does the configured PDF converter keep the brand font?

Converts a small sample document set in Larken, then reads the font names out of the PDF that comes back.
If Larken is there, reports will look as designed; if the service substituted another face, they will not,
however correct the numbers. Run it once after choosing a converter (and after any change to it):

    python -m chui_reporter.render.check_converter
"""

from __future__ import annotations

import io
import re
import tempfile
from pathlib import Path


def pdf_fonts(pdf: bytes) -> list[str]:
    from pypdf import PdfReader

    names: set[str] = set()

    def walk(font):
        font = font.get_object() if hasattr(font, "get_object") else font
        base = str(font.get("/BaseFont", ""))
        if base:
            names.add(re.sub(r"^[A-Z]{6}\+", "", base.lstrip("/")))
        for d in font.get("/DescendantFonts", []) or []:
            walk(d)

    for page in PdfReader(io.BytesIO(pdf)).pages:
        res = page.get("/Resources")
        fonts = (res.get_object().get("/Font") if res else None)
        if fonts:
            for f in fonts.get_object().values():
                walk(f)
    return sorted(names)


def sample_docx(dest: Path) -> Path:
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    for text, family, size, bold in [("Chui Ventures Fund I", "Larken-Bold", 20, True),
                                     ("Investor Report. The quick brown fox jumps over the lazy dog, 0123456789.", "Larken-Regular", 11, False),
                                     ("Italic line for the third face.", "Larken-Italic", 11, False)]:
        run = doc.add_paragraph().add_run(text)
        run.font.name, run.font.size, run.bold = family, Pt(size), bold
        run._element.rPr.rFonts.set("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}hAnsi", family)
    doc.save(str(dest))
    return dest


def check(store=None) -> dict:
    from .. import config
    from . import converters

    conv = converters.get_converter(store)
    conv.prepare(config.SOURCE_ROOT)
    with tempfile.TemporaryDirectory(prefix="check_") as tmp:
        docx = sample_docx(Path(tmp) / "font-check.docx")
        pdf = conv.convert(docx, Path(tmp))
        data = pdf.read_bytes()
    fonts = pdf_fonts(data)
    ok = any("larken" in f.lower() for f in fonts)
    return {"converter": conv.name, "fonts": fonts, "larken": ok,
            "verdict": ("Larken is in the PDF: reports will look as designed." if ok else
                        "Larken is NOT in the PDF. The converter substituted another face, so layout and brand will be wrong. "
                        "Larken's licence flag (fsType 4, preview and print only) is the usual reason a hosted converter refuses "
                        "it. Use LibreOffice, or a licence that permits embedding.")}


if __name__ == "__main__":
    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv(usecwd=True))
    res = check()
    print(f"converter: {res['converter']}\nfonts in the PDF: {', '.join(res['fonts']) or '(none found)'}\n{res['verdict']}")
    raise SystemExit(0 if res["larken"] else 1)
