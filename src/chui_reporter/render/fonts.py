"""Make the brand fonts available to LibreOffice.

The Larken files are licensed, so they are not in the repository or the container image.
They arrive the way everything else does: inside the Branding folder the user syncs. This
installs them for the current user if they are not already present, because LibreOffice
silently substitutes a missing font and a substituted font changes the pagination.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from .convert import ConversionError, assert_font_available


def install_brand_fonts(root: Path, family: str = "Larken") -> list[str]:
    """Install `family` from <root>/Branding/Fonts if it is missing. Returns the files copied."""
    try:
        assert_font_available(family)
        return []
    except ConversionError:
        pass
    src = Path(root) / "Branding" / "Fonts" / family
    files = [f for f in sorted(src.glob("*.ttf")) if "variable" not in f.name.casefold()] if src.exists() else []
    if not files:
        raise ConversionError(
            f"the {family} brand font is not installed and there is no Branding/Fonts/{family} "
            f"folder in the synced files to install it from")
    dest = (Path.home() / "Library/Fonts") if sys.platform == "darwin" else Path.home() / ".fonts" / "chui-brand"
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for f in files:
        target = dest / f.name
        if not target.exists() or target.stat().st_size != f.stat().st_size:
            shutil.copyfile(f, target)
            copied.append(f.name)
    if sys.platform != "darwin" and shutil.which("fc-cache"):
        subprocess.run(["fc-cache", "-f", str(dest)], check=False, capture_output=True, timeout=120)
    assert_font_available(family)
    return copied
