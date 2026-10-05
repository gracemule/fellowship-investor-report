"""Brand assets: logos, and the cover page background.

Everything is taken from the client's Branding folder, never redrawn. The one
thing generated here is the cover background -- the brand's own watermark device
(the mark, enlarged and cropped off the page, in a barely-lighter navy) on the
brand navy, which the Brand Guide's cover and inset slides show but do not
supply as a standalone asset.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageFilter

from .. import config

NAVY = (0x00, 0x22, 0x3B)           # Brand Guide, and the logo files' own background
A4_MM = (210.0, 297.0)
DPI = 200


def _branding(*parts: str) -> Path:
    p = config.BRANDING.joinpath(*parts)
    if not p.exists():
        raise FileNotFoundError(
            f"brand asset missing: {p}\nSet CHUI_SOURCE_ROOT, or restore the Branding folder.")
    return p


def logo_on_navy() -> Path:
    """White wordmark with the gradient mark; its background is exactly the cover navy."""
    return _branding("Logos", "PNGs", "CV_logo_02.png")


def logo_on_white() -> Path:
    return _branding("Logos", "PNGs", "CV_logo_01.png")


def cover_background(dest: Path, *, strength: float = 0.12) -> Path:
    """Full-bleed A4 cover: navy with the oversized mark as a faint watermark."""
    w = round(A4_MM[0] / 25.4 * DPI)
    h = round(A4_MM[1] / 25.4 * DPI)
    page = Image.new("RGB", (w, h), NAVY)

    # The mark as white strokes on the icon's orange field -> a mask of the strokes.
    icon = Image.open(_branding("Logos", "Icons", "PNGs", "CV_Icon_03.png")).convert("RGB")
    mask = icon.convert("L").point(lambda v: 255 if v > 205 else 0)
    bbox = mask.getbbox()
    mask = mask.crop(bbox).filter(ImageFilter.MaxFilter(7))      # heavier strokes at scale

    target_h = int(h * 0.92)
    mask = mask.resize((int(mask.width * target_h / mask.height), target_h), Image.LANCZOS)
    # Anchored low and to the right so it bleeds off the edges like the brand slides.
    x = w - int(mask.width * 0.62)
    y = h - int(mask.height * 0.80)
    tint = Image.new("RGB", mask.size, (0x1B, 0x4A, 0x6E))
    layer = Image.new("L", mask.size, 0)
    layer.paste(mask.point(lambda v: int(v * strength)))
    page.paste(tint, (x, y), layer)

    dest.parent.mkdir(parents=True, exist_ok=True)
    page.save(dest, "PNG", dpi=(DPI, DPI))
    return dest
