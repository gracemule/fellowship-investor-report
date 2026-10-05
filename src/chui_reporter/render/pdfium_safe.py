"""PDFium is not thread-safe.

The agent framework runs parallel tool calls on separate threads, and the web app renders page
images on request threads. Two threads inside PDFium at once crash the whole process (a native
segmentation fault, not a Python error), so every use of PDFium in this codebase goes through
this one process-wide lock. Hold it only for the PDFium work itself, never across a network call.
"""

from __future__ import annotations

import threading

PDFIUM_LOCK = threading.RLock()
# Word -> PDF conversions and render outputs share file names; one at a time.
RENDER_LOCK = threading.RLock()


def page_sizes(pdf: bytes) -> list[tuple[float, float]]:
    """Width and height of every page, in points."""
    import pypdfium2 as pdfium

    with PDFIUM_LOCK:
        doc = pdfium.PdfDocument(pdf)
        try:
            return [tuple(round(x, 1) for x in doc[i].get_size()) for i in range(len(doc))]
        finally:
            doc.close()
