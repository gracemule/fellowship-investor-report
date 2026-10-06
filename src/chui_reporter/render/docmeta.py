"""Document properties: a downloaded Word file carries no author.

The report is the fund manager's own document, so it must not name whoever or whatever produced it. The renderer leaves the
author blank, and this clears it from files stored before that was so (and anything else that slipped into the properties)."""

from __future__ import annotations

import io
import re
import zipfile

_CLEAR = (("dc:creator", ""), ("cp:lastModifiedBy", ""), ("dc:description", ""), ("cp:keywords", ""))


def blank_docx_author(blob: bytes) -> bytes:
    """The same Word file with the author (creator, last modified by) and stray comments/keywords emptied. Anything that is
    not a readable Word package is returned unchanged."""
    try:
        zin = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        return blob
    if "docProps/core.xml" not in zin.namelist():
        return blob
    out = io.BytesIO()
    with zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "docProps/core.xml":
                xml = data.decode("utf-8")
                for tag, value in _CLEAR:
                    xml = re.sub(rf"<{tag}(\s[^>]*)?>.*?</{tag}>", rf"<{tag}\1>{value}</{tag}>", xml, flags=re.S)
                    xml = re.sub(rf"<{tag}(\s[^>]*)?/>", rf"<{tag}\1/>", xml)
                data = xml.encode("utf-8")
            zout.writestr(item, data)
    return out.getvalue()
