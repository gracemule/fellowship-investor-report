# Phase 0 spike: can LibreOffice reproduce the report faithfully?

**Date:** 2026-10-05  **LibreOffice:** 25.8.7.3 (aarch64)  **Verdict: GO**

The plan put this first because a negative result invalidates the whole
rendering path. It is positive, with one finding that changes what we build.

## Method

Converted `Quarterly Reporting Template 2026.docx` with headless LibreOffice and
compared the output against the shipped `Chui Ventures Fund I - Q2 2026
Report.pdf` on page geometry, embedded fonts, and a rendered cover page.

## Results

### Page geometry survives

| | portrait | landscape |
| --- | --- | --- |
| Shipped Q2 PDF | 11 pages @ 596x842 | 6 pages @ 842x596 |
| LibreOffice output | 11 pages @ 595x842 | 8 pages @ 842x595 |

The mixed-orientation section breaks are preserved. The 1pt difference is
rounding. The extra landscape pages are the template's unshipped Capital
Accounts section, not a conversion artefact.

### Fonts: the shipped report is itself mixed

| Source | Fonts |
| --- | --- |
| Shipped Q2 PDF | PT Serif (4 faces), Calibri, Times New Roman, Arial |
| LibreOffice from template | PT Serif (4 faces), **Carlito** (4), Times New Roman |

The only substitution is Calibri -> Carlito. Carlito is a metric-compatible
Calibri clone -- identical advance widths, so line breaking and pagination are
unaffected. Calibri cannot be installed legitimately (proprietary), so this
substitution is the correct outcome rather than a problem to solve.

### The finding: the template is not the shipped design

Rendering the cover revealed the real issue, and it is not LibreOffice's.

**The shipped cover is set in PT Serif. The template's cover is not** -- it
leaves the theme defaults in place, which are the stock Office Calibri
Light/Calibri. The 1,032 direct PT Serif runs found in `document.xml` are in the
body and tables; the cover was never given the brand face.

So the shipped PDFs were produced from a document edited further than the
template we were handed. This is the same template-vs-delivered gap already
identified for section structure, now confirmed for typography.

### The fix, verified

Patching `theme1.xml` (major and minor latin typeface) and the `styles.xml`
font defaults to PT Serif, then reconverting:

- Carlito disappears entirely; output fonts are PT Serif (4 faces) + Times New Roman
- The cover renders in PT Serif and matches the shipped design

This is squarely within our control, because `skeleton.docx` is ours to build.

## Consequences for the plan

1. **The LibreOffice conversion path is viable.** No need to cost Gotenberg or a
   Word-based route.
2. **`skeleton.docx` must set PT Serif as the theme and style default**, not only
   as direct formatting. Relying on the template's own theme reproduces the
   wrong cover.
3. **Font presence must be asserted at startup.** `assert_font_available()` does
   this. A substituted font passes every numeric test while changing pagination.
4. **poppler is not needed.** `pypdfium2` rasterises with no system dependency,
   so it is dropped from the Docker image and from the plan.
5. Conversions stay serialised with a per-call `-env:UserInstallation` profile.

## Visual-regression harness

`chui_reporter.render.convert.compare_pdfs` renders each page and reports global
grayscale SSIM plus a pixel-difference ratio. Calibration:

| Comparison | SSIM | Pixel diff | Pass |
| --- | --- | --- | --- |
| A page against itself | 1.0000 | 0.00% | yes |
| Same layout, Calibri vs PT Serif | 0.1917 | 4.36% | no |

Thresholds are SSIM >= 0.98 and pixel diff <= 2%. A typeface swap -- the exact
regression that would otherwise ship silently -- is caught decisively.

## Reproducing

```bash
python -c "from chui_reporter.render.convert import assert_font_available, docx_to_pdf; \
           assert_font_available(); print(docx_to_pdf('template.docx', 'out/'))"
```

LibreOffice lives at `~/Applications/LibreOffice.app` (installed from the
official DMG without admin rights); PT Serif at `~/Library/Fonts`.
