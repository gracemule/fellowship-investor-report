"""Reading a web page, and keeping what was read as evidence.

A figure from the web is only usable if it can be checked later. So every page the agent reads is stored
as a snapshot (the text as fetched, with a hash and a time), and a figure is accepted only if the agent
cites an exact quotation from that snapshot that contains it. That is the same rule as for workbooks
and PDFs (the cited source is re-read and must really contain the number), applied to pages.

Safety: the server is asked to fetch URLs a model chose, so private and internal addresses are refused,
redirects are re-checked at every hop, and sizes and time are capped.
"""

from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from ..agent.store import Store
from .search import NoSearchAvailable, SearchRouter

MAX_BYTES = 3_000_000
MAX_TEXT = 300_000
MAX_REDIRECTS = 3
THIN = 1500                     # fewer characters than this from an HTML page usually means it is drawn by JavaScript
UA = "Mozilla/5.0 (compatible; ChuiReporter/1.0; +investor-reporting)"


class FetchError(Exception):
    pass


class Refused(FetchError):
    """The address itself is not allowed (private network, credentials, wrong scheme). Never retried elsewhere."""


def _check_host(host: str) -> None:
    if not host or "." not in host and host != "localhost":
        raise Refused("that address is not a public web page")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise FetchError(f"could not find {host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise Refused("that address points to a private or internal network and is not fetched")


def check_url(url: str) -> str:
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise Refused("only http(s) web addresses can be fetched")
    if u.username or u.password:
        raise Refused("addresses with credentials are not fetched")
    _check_host(u.hostname)
    return u.geturl()


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe"}
    BLOCK = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "tr", "table", "ul", "ol", "header", "footer"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in ("td", "th"):
            self.out.append(" | ")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.out.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    p = _Text()
    p.feed(html)
    text = "".join(p.out)
    text = re.sub(r"[ \t\r\f\v ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, re.sub(r"\s+", " ", p.title).strip()


def norm(s: str) -> str:
    """Whitespace-, case- and quote-insensitive form, for matching a quotation to a page."""
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip().casefold()


def _download(url: str, client: httpx.Client) -> tuple[str, bytes, str]:
    current = check_url(url)
    for _ in range(MAX_REDIRECTS + 1):
        try:
            with client.stream("GET", current, headers={"User-Agent": UA, "Accept": "text/html,application/pdf,application/json,text/plain,*/*"},
                               follow_redirects=False) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    current = check_url(urljoin(current, r.headers["location"]))
                    continue
                if r.status_code >= 400:
                    raise FetchError(f"the site answered {r.status_code}")
                body, size = io.BytesIO(), 0
                for chunk in r.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise FetchError("the page is larger than 3 MB")
                    body.write(chunk)
                return current, body.getvalue(), (r.headers.get("content-type") or "").split(";")[0].strip().lower()
        except httpx.HTTPError as exc:
            raise FetchError(f"could not reach the site ({type(exc).__name__})") from exc
    raise FetchError("too many redirects")


def _text_of(body: bytes, ctype: str, url: str) -> tuple[str, str]:
    if ctype == "application/pdf" or url.lower().split("?")[0].endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(body))
        return "\n\n".join((pg.extract_text() or "") for pg in reader.pages[:40]), ""
    raw = body.decode("utf-8", errors="replace")
    if "json" in ctype:
        try:
            return json.dumps(json.loads(raw), indent=1)[:MAX_TEXT], ""
        except ValueError:
            return raw, ""
    if "html" in ctype or raw.lstrip().lower().startswith(("<!doctype html", "<html")):
        return html_to_text(raw)
    return raw, ""


def save_snapshot(store: Store, url: str, text: str, title: str, via: str) -> str:
    text = text[:MAX_TEXT]
    sha = hashlib.sha256(text.encode()).hexdigest()
    with store.conn() as c:
        c.execute(f"INSERT INTO {store._t('web_snapshot')} (url, sha256, title, text, via) VALUES (%s,%s,%s,%s,%s) "
                  f"ON CONFLICT (url) DO UPDATE SET sha256=EXCLUDED.sha256, title=EXCLUDED.title, text=EXCLUDED.text, "
                  f"via=EXCLUDED.via, fetched_at=now()", (url, sha, title, text, via))
    return sha


def get_snapshot(store: Store, url: str) -> dict | None:
    with store.conn() as c:
        return c.execute(f"SELECT url, fetched_at, sha256, title, text, via FROM {store._t('web_snapshot')} "
                         f"WHERE url=%s", (url,)).fetchone()


def fetch(store: Store, url: str, *, router: SearchRouter | None = None, client: httpx.Client | None = None) -> dict:
    """Read a page and keep it. Returns {url, title, text, via}. A page that comes back nearly empty is
    probably drawn by JavaScript; if a provider that can render pages is available it is asked instead."""
    client = client or httpx.Client(timeout=20.0)
    via, final, text, title, ctype, failure = "direct", url, "", "", "", None
    try:
        final, body, ctype = _download(url, client)
        text, title = _text_of(body, ctype, final)
    except Refused:
        raise
    except FetchError as exc:                      # blocked, erroring or unreachable from here
        failure = exc
    # A page that is blocked to us, or comes back nearly empty (drawn by JavaScript), is asked of a provider
    # that renders pages, if one is available. Nothing is ever taken from it that is not then stored and quoted.
    if (failure is not None or len(text.strip()) < THIN) and router is not None and "pdf" not in ctype:
        try:
            _, pages = router.extract([final])
            rendered = (pages.get(final) or next(iter(pages.values()), "")).strip()
            if len(rendered) > len(text.strip()):
                text, via, failure = rendered, "tavily-extract", None
        except NoSearchAvailable:
            pass
    if failure is not None:
        raise failure
    if not text.strip():
        raise FetchError("the page had no readable text")
    save_snapshot(store, final, text, title, via)
    return {"url": final, "title": title, "text": text, "via": via}


def windows(text: str, find: str, width: int = 280, limit: int = 6) -> list[str]:
    out, low, needle = [], text.casefold(), find.casefold()
    start = 0
    while len(out) < limit:
        i = low.find(needle, start)
        if i < 0:
            break
        out.append(text[max(0, i - width): i + len(find) + width].strip())
        start = i + len(find) + width
    return out


SOCIAL_HOSTS = {"x.com", "twitter.com", "facebook.com", "fb.com", "instagram.com", "linkedin.com", "t.me",
                "tiktok.com", "youtube.com", "youtu.be", "reddit.com"}


def is_social(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return host in SOCIAL_HOSTS or any(host.endswith("." + h) for h in SOCIAL_HOSTS)


def verify_web_claim(f, store: Store | None) -> tuple[bool, str]:
    """A figure from the web is verified by its quotation: the quoted text must be on the page as it was
    fetched, and the figure must be written in the quotation."""
    from ..agent.ledger import _pdf_forms

    if store is None:
        return False, "no workspace to check the page against"
    if is_social(f.source_file):
        return False, ("a social media post is not accepted as the source of a figure in an investor report; "
                       "find the publisher's own page, bulletin or PDF")
    snap = get_snapshot(store, f.source_file)
    if not snap:
        return False, ("that page has not been fetched; read it with web_fetch first (a figure seen only in a "
                       "search result cannot be verified)")
    quote = (f.source_cell or "").strip()
    if len(quote) < 12:
        return False, "put the exact sentence or table row from the page that contains the figure in source_cell"
    if norm(quote) not in norm(snap["text"]):
        return False, "the quoted text does not appear on the page as it was fetched"
    for form in _pdf_forms(float(f.value)):
        if re.search(rf"(?<![\d.,]){re.escape(form)}(?![\d])", quote):
            return True, f"quotation found on the page fetched {snap['fetched_at']:%d %b %Y}; it contains {form!r}"
    return False, f"the quotation is on the page but does not contain {f.value!r} (or a unit form of it)"
