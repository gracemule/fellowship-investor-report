"""DOCX -> PDF conversion and page-level visual comparison.

Conversion is LibreOffice headless. Two operational notes carried over from the
spike and the research:

* Each call gets its own `-env:UserInstallation` profile. LibreOffice leaks
  memory and leaves zombie processes when several instances share a profile, so
  conversions are serialised and isolated.
* The font must be installed on the machine doing the conversion. LibreOffice
  substitutes silently, and a substituted font changes line breaking and
  pagination without changing a single number -- it would pass every numeric
  test while producing a visibly different document.

Comparison uses a grayscale structural-similarity score over the rendered page,
plus a raw pixel-difference ratio. Both are computed with numpy only; a page
that drifts beyond threshold fails the build the same way a bad figure does.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

SOFFICE_CANDIDATES = (
    Path.home() / "Applications/LibreOffice.app/Contents/MacOS/soffice",
    Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
    Path("/usr/bin/soffice"),
    Path("/usr/local/bin/soffice"),
    Path("/opt/homebrew/bin/soffice"),
)


class ConversionError(RuntimeError):
    pass


def clean_secret(value: str | None, name: str = "") -> str:
    """A secret as it was pasted into a settings box: without surrounding spaces or quotes, and without the 'NAME=' in front
    when the whole line of a .env file was copied."""
    v = (value or "").strip().strip("\"'").strip()
    if name and v.startswith(name + "="):
        v = v[len(name) + 1:].strip().strip("\"'").strip()
    return v


def fingerprint(secret: str) -> str:
    """Eight characters that identify a secret without revealing it, for comparing what two places hold."""
    import hashlib

    return hashlib.sha256(secret.encode()).hexdigest()[:8] if secret else "none"


def find_soffice() -> Path:
    for p in SOFFICE_CANDIDATES:
        if p.exists():
            return p
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return Path(found)
    raise ConversionError(
        "LibreOffice not found. Install it, or set one of: "
        + ", ".join(str(p) for p in SOFFICE_CANDIDATES)
    )


def assert_font_available(family: str = "Larken") -> None:
    """Fail loudly if the brand font is missing.

    A substituted font passes every numeric check while silently changing
    pagination, so this is asserted at startup rather than discovered in a diff.
    """
    roots = [Path.home() / "Library/Fonts", Path("/Library/Fonts"), Path("/usr/share/fonts"),
             Path("/usr/local/share/fonts"), Path.home() / ".local/share/fonts", Path.home() / ".fonts"]
    needle = family.replace(" ", "").casefold()
    for root in roots:
        if not root.exists():
            continue
        for f in root.rglob("*"):
            if f.suffix.lower() in {".ttf", ".otf", ".ttc"} and needle in f.name.replace(
                "_", "").replace(" ", "").casefold():
                return
    raise ConversionError(
        f"font {family!r} is not installed; LibreOffice would substitute it and "
        f"change pagination. Put the Larken files in the Branding folder you sync, or install "
        f"them to ~/Library/Fonts."
    )


def docx_to_pdf(docx: str | Path, out_dir: str | Path, *, timeout: int = 180) -> Path:
    docx, out_dir = Path(docx), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    soffice = find_soffice()
    with tempfile.TemporaryDirectory(prefix="lo_profile_") as profile:
        cmd = [
            str(soffice), "--headless", "--norestore",
            f"-env:UserInstallation=file://{profile}",
            "--convert-to", "pdf", "--outdir", str(out_dir), str(docx),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    pdf = out_dir / (docx.stem + ".pdf")
    if not pdf.exists():
        raise ConversionError(
            f"conversion produced no PDF\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return pdf


# -- rasterisation and comparison -------------------------------------------


@dataclass
class PageComparison:
    page: int
    ssim: float
    pixel_diff_ratio: float
    size_a: tuple[int, int]
    size_b: tuple[int, int]

    @property
    def passed(self) -> bool:
        return self.ssim >= 0.98 and self.pixel_diff_ratio <= 0.02


def render_pages(pdf: str | Path, scale: float = 1.4):
    import pypdfium2 as pdfium

    from .pdfium_safe import PDFIUM_LOCK

    with PDFIUM_LOCK:                      # PDFium is not thread-safe; render everything, then yield
        doc = pdfium.PdfDocument(str(pdf))
        try:
            images = [(i + 1, doc[i].render(scale=scale).to_pil().convert("L")) for i in range(len(doc))]
        finally:
            doc.close()
    yield from images


def _ssim(a, b) -> float:
    """Global structural similarity on grayscale arrays."""
    import numpy as np

    x, y = a.astype(np.float64), b.astype(np.float64)
    mu_x, mu_y = x.mean(), y.mean()
    vx, vy = x.var(), y.var()
    cov = ((x - mu_x) * (y - mu_y)).mean()
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    return float(
        ((2 * mu_x * mu_y + c1) * (2 * cov + c2))
        / ((mu_x**2 + mu_y**2 + c1) * (vx + vy + c2))
    )


def compare_pdfs(a: str | Path, b: str | Path, *, scale: float = 1.4) -> list[PageComparison]:
    """Page-by-page visual comparison of two PDFs."""
    import numpy as np

    out: list[PageComparison] = []
    for (i, ia), (_, ib) in zip(render_pages(a, scale), render_pages(b, scale)):
        if ia.size != ib.size:
            ib = ib.resize(ia.size)
        arr_a, arr_b = np.asarray(ia), np.asarray(ib)
        diff = np.abs(arr_a.astype(np.int16) - arr_b.astype(np.int16))
        out.append(
            PageComparison(
                page=i,
                ssim=_ssim(arr_a, arr_b),
                pixel_diff_ratio=float((diff > 16).mean()),
                size_a=ia.size,
                size_b=ib.size,
            )
        )
    return out
