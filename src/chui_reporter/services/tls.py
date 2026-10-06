"""Completing a certificate chain the way a browser does.

Some public sites (statistics offices among them) serve their own certificate but not the intermediate certificate that
links it to a trusted root. Browsers notice and download the missing piece from the address the certificate names
("CA Issuers"); Python's HTTP client does not, and reports a plain connection error. This does what a browser does:
read that address, download the intermediate, and verify again.

Verification is never switched off. The downloaded certificate is only added to the set of certificates the chain may be
built from; the chain must still end at a root we already trust, and the hostname must still match. A certificate that
does not chain to a trusted root fails exactly as before.
"""

from __future__ import annotations

import socket
import ssl
from collections.abc import Callable

import certifi
import httpx

CAFILE = certifi.where()
MAX_CERT_BYTES = 65_536
MAX_DEPTH = 3

# DER encoding of the object identifier 1.3.6.1.5.5.7.48.2 (caIssuers), as it appears in the "Authority Information Access"
# extension, followed by the access location: a context-specific [6] string holding the address.
_CA_ISSUERS = bytes.fromhex("06082b06010505073002")

_contexts: dict[str, ssl.SSLContext] = {}


def issuer_urls(der: bytes) -> list[str]:
    """The addresses a DER certificate names for its issuer's certificate."""
    urls, i = [], 0
    while (i := der.find(_CA_ISSUERS, i)) >= 0:
        i += len(_CA_ISSUERS)
        if i + 2 > len(der) or der[i] != 0x86:
            continue
        n, start = der[i + 1], i + 2
        if n & 0x80:                                           # long form: the next bytes hold the length
            k = n & 0x7F
            n, start = int.from_bytes(der[start:start + k], "big"), start + k
        url = der[start:start + n].decode("ascii", "ignore")
        if url.lower().startswith(("http://", "https://")):
            urls.append(url)
    return urls


def incomplete_chain(exc: BaseException | None) -> bool:
    """True if a connection failed only because the server did not send an intermediate certificate."""
    seen = 0
    while exc is not None and seen < 8:
        if isinstance(exc, ssl.SSLCertVerificationError) and (
                exc.verify_code in (20, 21) or "unable to get local issuer" in str(exc)):
            return True
        exc, seen = exc.__cause__ or exc.__context__, seen + 1
    return False


def _leaf(host: str, port: int = 443, timeout: float = 10.0) -> bytes:
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE      # only to read the certificate offered, never to trust it
    with socket.create_connection((host, port), timeout=timeout) as s, ctx.wrap_socket(s, server_hostname=host) as t:
        return t.getpeercert(binary_form=True) or b""


def _as_der(data: bytes) -> bytes:
    if b"-----BEGIN" in data:
        return ssl.PEM_cert_to_DER_cert(data.decode("ascii", "ignore"))
    return data


def context_for(host: str, download: Callable[[str], bytes], port: int = 443) -> ssl.SSLContext | None:
    """A context that trusts the usual roots and can also build a chain through the intermediates `host` points to.

    `download` fetches a URL (the caller applies its own address checks). None if the certificate names no issuer."""
    key = f"{host}:{port}"
    if key in _contexts:
        return _contexts[key]
    try:
        der = _leaf(host, port)
    except OSError:
        return None
    found: list[bytes] = []
    for _ in range(MAX_DEPTH):
        nxt = None
        for url in issuer_urls(der):
            try:
                data = download(url)
                if len(data) <= MAX_CERT_BYTES:
                    nxt = _as_der(data)
                    break
            except Exception:                                       # noqa: BLE001 - try the next address, or give up
                continue
        if not nxt:
            break
        found.append(nxt)
        der = nxt
    if not found:
        return None
    ctx = ssl.create_default_context(cafile=CAFILE)
    ctx.load_verify_locations(cadata="\n".join(ssl.DER_cert_to_PEM_cert(d) for d in found))
    _contexts[key] = ctx
    return ctx


def repaired_client(host: str, download: Callable[[str], bytes], timeout: float = 20.0, port: int = 443) -> httpx.Client | None:
    ctx = context_for(host, download, port)
    return httpx.Client(verify=ctx, timeout=timeout) if ctx else None
