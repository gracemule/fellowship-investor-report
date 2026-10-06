"""A Word-to-PDF service: LibreOffice behind one authenticated endpoint.

It runs on its own small host so that LibreOffice's memory (a few hundred MB while it converts) never competes with the
agent. It is deliberately tiny: it imports nothing from the rest of the app except the conversion helper, keeps no state,
and holds no secret except the token the app presents.

    POST /convert    body: a zip holding one .docx and, optionally, fonts/<name>.ttf; header Authorization: Bearer <token>
                     -> the PDF
    GET|HEAD /healthz    liveness (no work, safe to ping every few minutes)
    GET /selftest        converts a built-in sample and reports the time and memory used (no input, rate limited)

The brand font (Larken) is licensed and is not in the image. The app sends the three faces it needs with each request; they
are installed once per host (by size) and LibreOffice then lays the document out with the real font. Conversions run one
at a time, each in its own LibreOffice profile, and stray soffice processes are cleaned up after every one.
"""

from __future__ import annotations

import hmac
import io
import os
import re
import resource
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from ..render.convert import ConversionError, docx_to_pdf, find_soffice

MAX_BUNDLE = 40 * 1024 * 1024          # bytes in the request
MAX_FONTS = 12 * 1024 * 1024           # bytes of fonts once unzipped
QUEUE_WAIT = 150.0                     # seconds a request waits for its turn before being told to come back
SELFTEST_EVERY = 30.0
FONT_DIR = Path.home() / ".fonts" / "chui-brand"
_FONT_MAGIC = (b"\x00\x01\x00\x00", b"true", b"OTTO", b"ttcf")

_turn = threading.Lock()
_last_selftest = 0.0


class BadRequest(Exception):
    pass


def token_ok(header: str | None) -> bool:
    want = os.environ.get("CHUI_CONVERTER_TOKEN", "").strip()
    if not want:
        return False
    got = (header or "")
    got = got[7:].strip() if got.lower().startswith("bearer ") else ""
    return bool(got) and hmac.compare_digest(got.encode(), want.encode())


def install_fonts(files: dict[str, bytes]) -> int:
    """Install TrueType files for LibreOffice; returns how many were new. Anything that is not a font is ignored."""
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    new = 0
    for name, data in files.items():
        safe = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(name).name)
        if not safe.lower().endswith((".ttf", ".otf")) or data[:4] not in _FONT_MAGIC:
            continue
        target = FONT_DIR / safe
        if target.exists() and target.stat().st_size == len(data):
            continue
        target.write_bytes(data)
        new += 1
    if new and shutil.which("fc-cache"):
        subprocess.run(["fc-cache", "-f", str(FONT_DIR)], check=False, capture_output=True, timeout=120)
    return new


def unpack(bundle: bytes) -> tuple[str, bytes, dict[str, bytes]]:
    """(docx name, docx bytes, fonts) from the request's zip, with the sizes checked before anything is inflated."""
    try:
        z = zipfile.ZipFile(io.BytesIO(bundle))
    except zipfile.BadZipFile as exc:
        raise BadRequest("the request is not a zip file") from exc
    docs = [i for i in z.infolist() if i.filename.lower().endswith(".docx") and not i.is_dir()]
    if len(docs) != 1:
        raise BadRequest("the zip must hold exactly one .docx file")
    fonts = [i for i in z.infolist() if i.filename.lower().endswith((".ttf", ".otf")) and not i.is_dir()]
    if docs[0].file_size > MAX_BUNDLE or sum(i.file_size for i in fonts) > MAX_FONTS:
        raise BadRequest("the files in the zip are too large")
    return Path(docs[0].filename).name, z.read(docs[0]), {Path(i.filename).name: z.read(i) for i in fonts}


def _stray_soffice() -> None:
    if shutil.which("pkill"):
        subprocess.run(["pkill", "-f", "soffice.bin"], check=False, capture_output=True)


def _peak_mb() -> float:
    return round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024, 1)    # kilobytes on Linux


def convert(name: str, docx: bytes, fonts: dict[str, bytes]) -> bytes:
    """One conversion, alone. Raises TimeoutError if the turn does not come, ConversionError if LibreOffice fails."""
    if not _turn.acquire(timeout=QUEUE_WAIT):
        raise TimeoutError("busy")
    try:
        install_fonts(fonts)
        with tempfile.TemporaryDirectory(prefix="conv_") as tmp:
            src = Path(tmp) / (re.sub(r"[^A-Za-z0-9._ -]", "_", name) or "report.docx")
            src.write_bytes(docx)
            return docx_to_pdf(src, Path(tmp) / "out").read_bytes()
    finally:
        _stray_soffice()
        _turn.release()


def _sample_docx() -> bytes:
    """A minimal Word file made by hand, so the service needs no document library to test itself."""
    body = ("<w:p><w:r><w:rPr><w:rFonts w:ascii=\"Larken-Regular\" w:hAnsi=\"Larken-Regular\"/></w:rPr>"
            "<w:t>Chui Ventures Fund I. Self-test: the quick brown fox jumps over the lazy dog, 0123456789.</w:t></w:r></w:p>") * 40
    parts = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                               '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                               '<Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" '
                               'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                       'Target="word/document.xml"/></Relationships>',
        "word/document.xml": '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                             f"<w:body>{body}</w:body></w:document>",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in parts.items():
            z.writestr(n, d)
    return buf.getvalue()


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.api_route("/healthz", methods=["GET", "HEAD"])
    def healthz():
        try:
            find_soffice()
            ok = True
        except ConversionError:
            ok = False
        return JSONResponse({"ok": True, "libreoffice": ok})

    @app.post("/convert")
    async def convert_route(request: Request):
        if not os.environ.get("CHUI_CONVERTER_TOKEN", "").strip():
            return JSONResponse({"error": "the converter has no token configured"}, status_code=503)
        if not token_ok(request.headers.get("authorization")):
            return JSONResponse({"error": "not authorised"}, status_code=401)
        declared = int(request.headers.get("content-length") or 0)
        if declared > MAX_BUNDLE + MAX_FONTS:
            return JSONResponse({"error": "the request is too large"}, status_code=413)
        data = await request.body()
        try:
            name, docx, fonts = unpack(data)
            pdf = await run_in_threadpool(convert, name, docx, fonts)
        except BadRequest as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except TimeoutError:
            return JSONResponse({"error": "the converter is busy"}, status_code=503, headers={"Retry-After": "20"})
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "the conversion took too long"}, status_code=504)
        except ConversionError as exc:
            return JSONResponse({"error": str(exc)[:400]}, status_code=500)
        return Response(pdf, media_type="application/pdf")

    @app.get("/selftest")
    async def selftest():
        """Convert a fixed sample and say how long it took and how much memory LibreOffice peaked at. Takes no input."""
        global _last_selftest
        now = time.time()
        if now - _last_selftest < SELFTEST_EVERY:
            return JSONResponse({"error": "try again in a moment"}, status_code=429, headers={"Retry-After": "30"})
        _last_selftest = now
        started = time.time()
        try:
            pdf = await run_in_threadpool(convert, "selftest.docx", _sample_docx(), {})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}, status_code=500)
        larken = FONT_DIR.exists() and any("larken" in f.name.casefold() for f in FONT_DIR.iterdir())
        return {"ok": pdf[:4] == b"%PDF", "seconds": round(time.time() - started, 1), "pdf_bytes": len(pdf),
                "libreoffice_peak_mb": _peak_mb(), "larken_installed": larken}

    return app


app = create_app()
