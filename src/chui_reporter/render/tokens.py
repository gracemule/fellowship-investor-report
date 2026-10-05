"""Design tokens, lifted from the template's own direct formatting.

The template's theme is the stock Office one; the entire brand lives in direct
formatting, so these are applied explicitly on every run and cell rather than
relied on through named styles.
"""

from __future__ import annotations

from docx.shared import Pt, RGBColor

FONT = "PT Serif"

NAVY = RGBColor(0x1A, 0x1A, 0x2E)        # table header fills, H1/H2
NAVY_COVER = RGBColor(0x1B, 0x32, 0x52)  # cover title, banner rows
NAVY_DEEP = RGBColor(0x00, 0x22, 0x3B)
ORANGE = RGBColor(0xE8, 0x61, 0x1A)
ORANGE_DARK = RGBColor(0xD4, 0x62, 0x2A)
INK = RGBColor(0x0F, 0x11, 0x15)
BODY = RGBColor(0x2C, 0x2C, 0x2C)
MUTED = RGBColor(0x8C, 0x9A, 0xAA)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)

# Hex strings for OOXML shading/borders, which take strings not RGBColor
HEX_NAVY = "1A1A2E"
HEX_NAVY_COVER = "1B3252"
HEX_ORANGE = "E8611A"
HEX_ORANGE_TINT = "FEF0E7"
HEX_BAND = "F4F4F4"
HEX_BAND_ALT = "EAECEE"
HEX_RULE = "CCCCCC"
HEX_WHITE = "FFFFFF"

SIZE_COVER_TITLE = Pt(24)
SIZE_COVER_QUARTER = Pt(26)
SIZE_COVER_SUB = Pt(16)
SIZE_COVER_FUND = Pt(18)
SIZE_H1 = Pt(14)
SIZE_H2 = Pt(11)
SIZE_BODY = Pt(10)
SIZE_TABLE = Pt(8.5)
SIZE_SMALL = Pt(9)

CHART_PALETTE = ["1A1A2E", "E8611A", "44546A", "ED7D31", "5B9BD5"]
