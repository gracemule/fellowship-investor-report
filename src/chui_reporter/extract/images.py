"""Images the user gives the agent: which kinds are accepted, and how any of them becomes something the model can read.

DeepSeek reads images natively, so nothing here is about the model's limits. It is about the files people actually have: a phone
photo is HEIC and may be stored rotated, a screenshot may be a PNG with a transparent background (which turns black behind black
text), a scan may be a TIFF or a 16-bit PNG, a GIF may be animated. Every one is normalised to the same plain, upright, opaque JPEG
before it is sent, so no format is a reason for the model to see something different from what the person sees.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

# One list for every part of the system (attaching, the tool that looks at an image, the ledger's rule for figures read from one).
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".jpe", ".jfif", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif", ".heic", ".heif"}


class ImageError(ValueError):
    """The image could not be read; the message says why in words a person can act on."""


def _register() -> None:
    from PIL import Image, ImageFile

    ImageFile.LOAD_TRUNCATED_IMAGES = True            # a photo cut short by a bad upload is still worth reading
    Image.MAX_IMAGE_PIXELS = 250_000_000              # large scans are normal; a "decompression bomb" is far beyond this
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except ImportError:                               # HEIC then reports itself as unsupported rather than as corrupt
        pass


def _opaque_rgb(im):
    """RGB without losing what was on the page: transparency goes onto white (not black), 16-bit and palette images are scaled."""
    from PIL import Image

    if im.mode in ("I;16", "I;16L", "I;16B", "I;16N", "I"):
        im = im.point(lambda p: p * (1 / 256)).convert("L")
    elif im.mode == "F":
        im = im.convert("L")
    if im.mode in ("RGBA", "LA", "PA", "La", "RGBa") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        page = Image.new("RGB", rgba.size, "white")
        page.paste(rgba, mask=rgba.getchannel("A"))
        return page
    return im.convert("RGB")


def to_model_jpeg(source: bytes | str | Path, *, max_side: int = 1600, quality: int = 85) -> str:
    """Any supported image -> a `data:image/jpeg;base64,...` URL: upright, opaque, the first frame, at most `max_side` wide or tall."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    _register()
    name = source.name if isinstance(source, Path) else (str(source) if isinstance(source, str) else "the image")
    try:
        opened = Image.open(io.BytesIO(source) if isinstance(source, bytes) else source)
        with opened as im:
            im.load()
            first = ImageOps.exif_transpose(im)       # phones store the picture sideways and say so in a tag
            page = _opaque_rgb(first)
            page.thumbnail((max_side, max_side), Image.LANCZOS)
            out = io.BytesIO()
            page.save(out, "JPEG", quality=quality, optimize=True)
    except UnidentifiedImageError as exc:
        suffix = Path(name).suffix.lower()
        hint = (" (HEIC photos need a decoder that is not installed on this server)" if suffix in (".heic", ".heif") else "")
        raise ImageError(f"{Path(name).name} is not an image this server can read{hint}.") from exc
    except Image.DecompressionBombError as exc:
        raise ImageError(f"{Path(name).name} is far larger than a photo or scan should be.") from exc
    except (OSError, ValueError, SyntaxError) as exc:
        raise ImageError(f"{Path(name).name} could not be opened ({type(exc).__name__}: {exc}).") from exc
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()
