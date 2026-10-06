"""Turning the Word file into the PDF: a local converter or a hosted one.

LibreOffice (local) is the reference: it is free, private (nothing leaves the machine) and honours the
installed brand font. It needs about 400 MB of memory while it runs, which a small host may not have, so it can also run as
a service of its own (RemoteLibreOfficeConverter, src/chui_reporter/converter): same renderer, separate host.

iLoveAPI (hosted) needs no installation, but three things are different and the code treats them as
first-class concerns rather than hiding them:

* Fonts. The service lists a fixed set of fonts and says other fonts "could cause problems". The report is
  set in Larken, so for this converter the font is embedded in a *temporary copy* of the Word file that is
  sent for conversion (the Word file you download is never embedded). Whether the service honours an
  embedded font is verified by `check_converter`, which converts a sample and reads the fonts out of the
  PDF it gets back. Larken permits embedding for preview and printing only (fsType 4), which is why the
  embedded copy is transient and the PDF is the product.
* Credit. Free accounts have a monthly file allowance. Identical documents are never converted twice,
  usage is counted, and an exhausted allowance is reported as such.
* Confidentiality. The unfinished report leaves the server for the conversion (the provider states that
  files are deleted after an hour; the task is also deleted as soon as the PDF is downloaded).
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

import httpx

from .convert import ConversionError, assert_font_available, clean_secret, docx_to_pdf, find_soffice

ILOVE_AUTH = "https://api.ilovepdf.com/v1/auth"
ILOVE_START = "https://api.ilovepdf.com/v1/start/officepdf"
FAMILY_FILES = {"Larken-Regular": "Larken Regular.ttf", "Larken-Bold": "Larken Bold.ttf",
                "Larken-Italic": "Larken Italic.ttf"}


class ConverterQuota(ConversionError):
    """The hosted converter's allowance is used up."""


# ---- embedding a font in a Word file ----------------------------------------------------------

_REL_FONT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/font"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def obfuscate(font: bytes, guid: uuid.UUID) -> bytes:
    """ECMA-376 font obfuscation: XOR the first 32 bytes with the GUID's bytes, reversed."""
    key = bytes.fromhex(guid.hex)[::-1]
    head = bytes(b ^ key[i % 16] for i, b in enumerate(font[:32]))
    return head + font[32:]


def embed_fonts(src: Path, dst: Path, fonts: dict[str, bytes]) -> Path:
    """Write a copy of the Word file with `fonts` ({family name: TTF bytes}) embedded as regular faces."""
    with zipfile.ZipFile(src) as zin:
        entries = {i.filename: zin.read(i.filename) for i in zin.infolist()}
    table = entries.get("word/fontTable.xml", b'<?xml version="1.0" encoding="UTF-8"?><w:fonts xmlns:w="%s"/>' % _W.encode()).decode()
    if 'xmlns:r=' not in table.split(">", 2)[1] and "xmlns:r=" not in table[:600]:
        table = table.replace("<w:fonts ", f'<w:fonts xmlns:r="{_R}" ', 1)
    rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', f'<Relationships xmlns="{_REL_NS}">']
    fonts_xml = ""
    for i, (family, data) in enumerate(fonts.items(), 1):
        guid = uuid.uuid4()
        entries[f"word/fonts/font{i}.odttf"] = obfuscate(data, guid)
        rid = f"rIdFont{i}"
        rels.append(f'<Relationship Id="{rid}" Type="{_REL_FONT}" Target="fonts/font{i}.odttf"/>')
        fonts_xml += (f'<w:font w:name="{family}"><w:charset w:val="00"/><w:family w:val="roman"/><w:pitch w:val="variable"/>'
                      f'<w:embedRegular r:id="{rid}" w:fontKey="{{{str(guid).upper()}}}"/></w:font>')
    rels.append("</Relationships>")
    # a family already declared in the table would be declared twice; drop the old declaration
    for family in fonts:
        table = re.sub(rf'<w:font w:name="{re.escape(family)}">.*?</w:font>', "", table, flags=re.S)
    entries["word/fontTable.xml"] = table.replace("</w:fonts>", fonts_xml + "</w:fonts>").encode()
    entries["word/_rels/fontTable.xml.rels"] = "".join(rels).encode()
    ct = entries["[Content_Types].xml"].decode()
    if 'Extension="odttf"' not in ct:
        ct = ct.replace("<Default ", '<Default Extension="odttf" ContentType="application/vnd.openxmlformats-officedocument.obfuscatedFont"/><Default ', 1)
    entries["[Content_Types].xml"] = ct.encode()
    settings = entries.get("word/settings.xml", b"").decode()
    if settings and "embedTrueTypeFonts" not in settings:
        entries["word/settings.xml"] = re.sub(r"(<w:settings[^>]*>)", r"\1<w:embedTrueTypeFonts/>", settings, count=1).encode()
    dst.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        zout.writestr("[Content_Types].xml", entries.pop("[Content_Types].xml"))     # must come first
        for name, data in entries.items():
            zout.writestr(name, data)
    return dst


def font_files(root: Path | None = None) -> dict[str, bytes]:
    """The Larken faces the report uses, as bytes, from the synced Branding folder or an installed copy."""
    from .. import config

    homes = [Path(root or config.SOURCE_ROOT) / "Branding" / "Fonts" / "Larken", Path.home() / "Library/Fonts",
             Path.home() / ".fonts" / "chui-brand", Path("/usr/share/fonts"), Path("/usr/local/share/fonts")]
    out = {}
    for family, fname in FAMILY_FILES.items():
        for h in homes:
            f = h / fname
            if f.exists():
                out[family] = f.read_bytes()
                break
    return out


# ---- converters --------------------------------------------------------------------------------


class LibreOfficeConverter:
    name = "libreoffice"
    remote = False

    def prepare(self, root: Path) -> None:
        from .fonts import install_brand_fonts

        find_soffice()
        install_brand_fonts(root)

    def convert(self, docx: Path, out_dir: Path) -> Path:
        assert_font_available("Larken")
        return docx_to_pdf(docx, out_dir)


class RemoteLibreOfficeConverter:
    """LibreOffice running as a service of its own (src/chui_reporter/converter): the same renderer as the local one, on a
    separate host so its memory never competes with the agent. The licensed brand font is sent with each request (three small
    files); the service installs it once and LibreOffice then lays the document out with the real font."""

    name = "remote"
    remote = True
    RETRY_WAIT = (4, 8, 15, 25, 30, 30, 30)       # a sleeping free host takes a minute or two to wake

    def __init__(self, url: str | None = None, token: str | None = None, *, client: httpx.Client | None = None,
                 sleep=time.sleep):
        self.url = (url or os.environ.get("CHUI_CONVERTER_URL", "")).strip().rstrip("/")
        self.token = clean_secret(token or os.environ.get("CHUI_CONVERTER_TOKEN", ""), "CHUI_CONVERTER_TOKEN")
        self.client = client or httpx.Client(timeout=httpx.Timeout(300.0, connect=30.0))
        self._sleep = sleep
        self.fonts: dict[str, bytes] = {}

    def prepare(self, root: Path) -> None:
        if not self.url or not self.token:
            raise ConversionError("CHUI_CONVERTER_URL and CHUI_CONVERTER_TOKEN must both be set to use the converter service")
        self.fonts = font_files(root)
        missing = [f for f in FAMILY_FILES if f not in self.fonts]
        if missing:
            raise ConversionError("the Larken font files are missing (" + ", ".join(missing) + "); add the Branding "
                                  "folder (with Fonts/Larken) to your folder so they can be sent to the converter")

    def _bundle(self, docx: Path) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(docx, docx.name)
            for family, data in self.fonts.items():
                z.writestr(f"fonts/{FAMILY_FILES[family]}", data)
        return buf.getvalue()

    def convert(self, docx: Path, out_dir: Path) -> Path:
        docx, out_dir = Path(docx), Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        body, last = self._bundle(docx), None
        for attempt in range(len(self.RETRY_WAIT) + 1):
            try:
                r = self.client.post(f"{self.url}/convert", content=body, headers={
                    "Authorization": f"Bearer {self.token}", "Content-Type": "application/zip"})
            except httpx.HTTPError as exc:                 # the host may be waking up
                last = ConversionError(f"could not reach the converter ({type(exc).__name__})")
            else:
                if r.status_code == 200 and r.content[:4] == b"%PDF":
                    out = out_dir / (docx.stem + ".pdf")
                    out.write_bytes(r.content)
                    return out
                if r.status_code in (401, 403):
                    raise ConversionError("the converter rejected the token; CHUI_CONVERTER_TOKEN must match on both services")
                if r.status_code in (400, 413):
                    raise ConversionError(f"the converter refused the file ({r.status_code}): {r.text[:160]}")
                last = ConversionError(f"the converter answered {r.status_code}: {r.text[:160]}")
                if r.status_code not in (500, 502, 503, 504):
                    break
            if attempt < len(self.RETRY_WAIT):
                self._sleep(self.RETRY_WAIT[attempt])
        raise last or ConversionError("the converter did not answer")


class ILoveApiConverter:
    name = "iloveapi"
    remote = True

    def __init__(self, public_key: str | None = None, *, client: httpx.Client | None = None, embed: bool | None = None,
                 on_use=None, cache_dir: Path | None = None, sleep=time.sleep):
        self.key = (public_key or os.environ.get("ILOVEAPI_PUBLIC_KEY", "")).strip()
        self.client = client or httpx.Client(timeout=120.0)
        self.embed = (os.environ.get("CHUI_EMBED_FONTS", "1") != "0") if embed is None else embed
        self.on_use = on_use                      # called with the credits left, when the service says
        self.cache_dir = cache_dir
        self._sleep = sleep
        self._token: tuple[str, float] | None = None
        self.fonts: dict[str, bytes] = {}

    def prepare(self, root: Path) -> None:
        if not self.key:
            raise ConversionError("ILOVEAPI_PUBLIC_KEY is not set")
        if self.embed:
            self.fonts = font_files(root)
            missing = [f for f in FAMILY_FILES if f not in self.fonts]
            if missing:
                raise ConversionError("the Larken font files are missing (" + ", ".join(missing) + "); add the Branding "
                                      "folder (with Fonts/Larken) to your folder so they can be embedded for conversion")

    # -- protocol
    def _bearer(self) -> str:
        if self._token and self._token[1] > time.time():
            return self._token[0]
        r = self.client.post(ILOVE_AUTH, data={"public_key": self.key})
        if r.status_code != 200:
            raise ConversionError(f"iLoveAPI rejected the key ({r.status_code}); check ILOVEAPI_PUBLIC_KEY")
        self._token = (r.json()["token"], time.time() + 50 * 60)
        return self._token[0]

    def _call(self, method: str, url: str, *, tolerate: tuple[int, ...] = (), **kw) -> httpx.Response:
        last = None
        for attempt in range(3):
            headers = {"Authorization": f"Bearer {self._bearer()}"}
            try:
                r = self.client.request(method, url, headers=headers, **kw)
            except httpx.HTTPError as exc:
                last = ConversionError(f"could not reach iLoveAPI ({type(exc).__name__})")
                self._sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 401:
                self._token = None
                last = ConversionError("iLoveAPI rejected the token")
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = ConversionError(f"iLoveAPI is busy ({r.status_code})")
                self._sleep(2.0 * (attempt + 1))
                continue
            if r.status_code in tolerate:
                return r
            if r.status_code >= 400:
                text = r.text[:300]
                if re.search(r"credit|limit|quota|plan", text, re.I):
                    raise ConverterQuota(f"the iLoveAPI allowance is used up ({text.strip()[:120]})")
                raise ConversionError(f"iLoveAPI refused the request ({r.status_code}): {text.strip()[:160]}")
            return r
        raise last or ConversionError("iLoveAPI did not answer")

    def _convert_bytes(self, data: bytes, name: str) -> bytes:
        r = self._call("GET", ILOVE_START, tolerate=(404, 405))    # the documentation shows POST, older clients GET
        if r.status_code in (404, 405):
            r = self._call("POST", ILOVE_START)
        start = r.json()
        server, task = start["server"], start["task"]
        if self.on_use and "remaining_credits" in start:
            self.on_use(start["remaining_credits"])
        try:
            up = self._call("POST", f"https://{server}/v1/upload", data={"task": task}, files={"file": (name, data)}).json()
            done = self._call("POST", f"https://{server}/v1/process", json={
                "task": task, "tool": "officepdf", "files": [{"server_filename": up["server_filename"], "filename": name}]}).json()
            if done.get("status") not in (None, "TaskSuccess"):
                raise ConversionError(f"iLoveAPI could not convert the file ({done.get('status')})")
            body = self._call("GET", f"https://{server}/v1/download/{task}").content
        finally:
            try:                                   # do not leave the unfinished report on their server
                self.client.request("DELETE", f"https://{server}/v1/task/{task}", headers={"Authorization": f"Bearer {self._bearer()}"})
            except Exception:  # noqa: BLE001
                pass
        if body[:4] == b"PK\x03\x04":              # several outputs come back zipped
            with zipfile.ZipFile(io.BytesIO(body)) as z:
                pdfs = [n for n in z.namelist() if n.lower().endswith(".pdf")]
                if not pdfs:
                    raise ConversionError("iLoveAPI returned an archive with no PDF")
                body = z.read(pdfs[0])
        if body[:4] != b"%PDF":
            raise ConversionError("iLoveAPI did not return a PDF")
        return body

    def convert(self, docx: Path, out_dir: Path) -> Path:
        docx, out_dir = Path(docx), Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="conv_") as tmp:
            sendable = embed_fonts(docx, Path(tmp) / docx.name, self.fonts) if (self.embed and self.fonts) else docx
            data = sendable.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            cache = (self.cache_dir or out_dir / ".conversions") / f"{digest}.pdf"
            if cache.exists():                      # the same document is never converted (or paid for) twice
                pdf = cache.read_bytes()
            else:
                pdf = self._convert_bytes(data, docx.name)
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_bytes(pdf)
                self._count()
        out = out_dir / (docx.stem + ".pdf")
        out.write_bytes(pdf)
        return out

    def _count(self) -> None:
        if self.on_use:
            self.on_use(None)


def get_converter(store=None):
    """The converter named by CHUI_PDF_CONVERTER (libreoffice | iloveapi | auto). `auto` prefers the local
    one when it is installed, so a machine that has LibreOffice never sends a document anywhere."""
    from ..services import usage

    choice = os.environ.get("CHUI_PDF_CONVERTER", "auto").strip().lower()

    def on_use(left):
        if store is None:
            return
        if left is None:
            usage.record(store, "iloveapi")
        else:
            usage.set_state(store, "iloveapi", "ok", detail=f"{left} credits left")

    def ilove():
        return ILoveApiConverter(on_use=on_use)

    if choice == "libreoffice":
        return LibreOfficeConverter()
    if choice == "remote":
        return RemoteLibreOfficeConverter()
    if choice == "iloveapi":
        return ilove()
    try:
        find_soffice()
        return LibreOfficeConverter()
    except ConversionError:
        if os.environ.get("CHUI_CONVERTER_URL", "").strip():
            return RemoteLibreOfficeConverter()
        if os.environ.get("ILOVEAPI_PUBLIC_KEY", "").strip():
            return ilove()
        raise ConversionError("no PDF converter is available: install LibreOffice, set CHUI_CONVERTER_URL and "
                              "CHUI_CONVERTER_TOKEN, or set ILOVEAPI_PUBLIC_KEY (and CHUI_PDF_CONVERTER=iloveapi)") from None


def describe() -> dict:
    """Which converter would be used, and whether it can be, for the interface (no network calls)."""
    choice = os.environ.get("CHUI_PDF_CONVERTER", "auto").strip().lower()
    local = True
    try:
        find_soffice()
    except ConversionError:
        local = False
    key = bool(os.environ.get("ILOVEAPI_PUBLIC_KEY", "").strip())
    remote_set = bool(os.environ.get("CHUI_CONVERTER_URL", "").strip() and os.environ.get("CHUI_CONVERTER_TOKEN", "").strip())
    if choice == "libreoffice" or (choice == "auto" and local):
        name = "libreoffice"
    elif choice == "remote" or (choice == "auto" and remote_set):
        name = "remote"
    else:
        name = "iloveapi"
    ready = {"libreoffice": local, "remote": remote_set, "iloveapi": key}[name]
    return {"name": name, "ready": ready, "choice": choice, "libreoffice_installed": local, "iloveapi_key": key,
            "remote_configured": remote_set}
