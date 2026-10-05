"""Page images of the stored report PDF, for the viewer."""

from __future__ import annotations

import io
import threading

from ..render.pdfium_safe import PDFIUM_LOCK
from collections import OrderedDict

_CACHE: OrderedDict[tuple, bytes] = OrderedDict()
_LOCK = threading.Lock()
_MAX = 80


def render_page(pdf: bytes, n: int, *, key: tuple, scale: float = 2.0) -> bytes:
    """PNG of page `n` (1-based). Cached by `key` (version, page, scale): a version never changes."""
    ck = (*key, n, scale)
    with _LOCK:
        if ck in _CACHE:
            _CACHE.move_to_end(ck)
            return _CACHE[ck]
    import pypdfium2 as pdfium

    with PDFIUM_LOCK:
        doc = pdfium.PdfDocument(pdf)
        try:
            if not 1 <= n <= len(doc):
                raise IndexError(n)
            img = doc[n - 1].render(scale=scale).to_pil().convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "PNG", optimize=False)
            data = buf.getvalue()
        finally:
            doc.close()
    with _LOCK:
        _CACHE[ck] = data
        while len(_CACHE) > _MAX:
            _CACHE.popitem(last=False)
    return data


def page_sizes(pdf: bytes) -> list[tuple[float, float]]:
    from ..render.pdfium_safe import page_sizes as _sizes

    return _sizes(pdf)
