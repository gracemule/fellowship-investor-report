"""PDFium crashes the whole process (a segmentation fault) if two threads use it at once. The agent
framework runs parallel tool calls on threads, and the web app serves page images on request threads,
so this was found the hard way: a server killed mid-run while the agent inspected two pages at once."""

from __future__ import annotations

import io
import threading
import time

from pypdf import PdfWriter

from chui_reporter.app import pages
from chui_reporter.render.pdfium_safe import PDFIUM_LOCK


def _pdf(n=3) -> bytes:
    w = PdfWriter()
    for _ in range(n):
        w.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_concurrent_page_renders_all_succeed():
    pdf, out, errors = _pdf(), [], []

    def work(i):
        try:
            out.append(pages.render_page(pdf, 1 + i % 3, key=("t", i), scale=1.0))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(24)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(out) == 24 and all(b[:4] == b"\x89PNG" for b in out)


def test_rendering_goes_through_the_process_wide_lock():
    pdf, finished = _pdf(1), threading.Event()
    t = threading.Thread(target=lambda: (pages.render_page(pdf, 1, key=("lock", 0), scale=1.0), finished.set()))
    with PDFIUM_LOCK:
        t.start()
        time.sleep(0.4)
        assert not finished.is_set(), "a render started while another thread held PDFium"
    t.join(timeout=10)
    assert finished.is_set()


def test_page_sizes_are_read_under_the_lock():
    assert pages.page_sizes(_pdf(2)) == [(595.0, 842.0), (595.0, 842.0)]
