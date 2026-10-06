"""A site that omits its intermediate certificate is reachable (as in a browser) without ever switching verification off."""

from __future__ import annotations

import http.server
import shutil
import ssl
import subprocess
import threading

import httpx
import pytest

from chui_reporter.services import tls, web

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="the openssl command is needed to make test certificates")


def _run(*args, cwd):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


class _Quiet(http.server.BaseHTTPRequestHandler):
    body, ctype = b"", "text/html"

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", self.ctype)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a):
        pass


def _serve(body: bytes, ctype: str, context: ssl.SSLContext | None = None):
    handler = type("H", (_Quiet,), {"body": body, "ctype": ctype})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    if context:
        srv.socket = context.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture()
def site(tmp_path, monkeypatch):
    """root -> intermediate -> leaf for 'localhost'; the HTTPS server sends only the leaf. The intermediate is published
    at an address named in the leaf's Authority Information Access, as real CAs do."""
    d = tmp_path
    (d / "ca.ext").write_text("basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n"
                              "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n")
    (d / "root.cnf").write_text("[req]\ndistinguished_name=dn\nx509_extensions=v3\nprompt=no\n[dn]\nCN=Test Root\n"
                                "[v3]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n")
    _run("genrsa", "-out", "root.key", "2048", cwd=d)
    _run("req", "-x509", "-new", "-key", "root.key", "-sha256", "-days", "2", "-config", "root.cnf", "-out", "root.crt", cwd=d)
    _run("genrsa", "-out", "inter.key", "2048", cwd=d)
    _run("req", "-new", "-key", "inter.key", "-subj", "/CN=Test Intermediate", "-out", "inter.csr", cwd=d)
    _run("x509", "-req", "-in", "inter.csr", "-CA", "root.crt", "-CAkey", "root.key", "-CAcreateserial", "-days", "2",
         "-sha256", "-extfile", "ca.ext", "-out", "inter.crt", cwd=d)
    _run("x509", "-in", "inter.crt", "-outform", "DER", "-out", "inter.der", cwd=d)
    pki = _serve((d / "inter.der").read_bytes(), "application/pkix-cert")          # where the leaf says its issuer is
    (d / "leaf.ext").write_text("basicConstraints=CA:FALSE\nsubjectAltName=DNS:localhost\nkeyUsage=digitalSignature,keyEncipherment\n"
                                "extendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n"
                                f"authorityInfoAccess=caIssuers;URI:http://localhost:{pki.server_port}/inter.der\n")
    _run("genrsa", "-out", "leaf.key", "2048", cwd=d)
    _run("req", "-new", "-key", "leaf.key", "-subj", "/CN=localhost", "-out", "leaf.csr", cwd=d)
    _run("x509", "-req", "-in", "leaf.csr", "-CA", "inter.crt", "-CAkey", "inter.key", "-CAcreateserial", "-days", "2",
         "-sha256", "-extfile", "leaf.ext", "-out", "leaf.crt", cwd=d)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(d / "leaf.crt", d / "leaf.key")                      # the leaf only: no intermediate sent
    https = _serve(b"<html><head><title>Policy rate</title></head><body>" + b"The policy rate was 8.75 percent in June 2026. " * 40
                   + b"</body></html>", "text/html", server_ctx)
    monkeypatch.setattr(web, "_check_host", lambda host: None)                       # the test server is on this machine
    monkeypatch.setattr(tls, "CAFILE", str(d / "root.crt"))
    tls._contexts.clear()
    yield {"url": f"https://localhost:{https.server_port}/", "root": str(d / "root.crt"), "leaf_der": ssl.PEM_cert_to_DER_cert((d / "leaf.crt").read_text())}
    https.shutdown()
    pki.shutdown()
    tls._contexts.clear()


def test_the_address_of_the_missing_intermediate_is_read_from_the_certificate(site):
    urls = tls.issuer_urls(site["leaf_der"])
    assert len(urls) == 1 and urls[0].startswith("http://localhost:") and urls[0].endswith("/inter.der")


def test_a_certificate_with_no_issuer_address_names_none():
    assert tls.issuer_urls(b"\x30\x03\x02\x01\x01") == []
    long_form = tls._CA_ISSUERS + bytes([0x86, 0x81, 0x84]) + b"http://pki.example/" + b"a" * 109
    assert tls.issuer_urls(b"xx" + long_form)[0].startswith("http://pki.example/a")


def test_a_site_that_omits_its_intermediate_is_read_with_the_chain_still_verified(site):
    trusting_root_only = httpx.Client(verify=ssl.create_default_context(cafile=site["root"]))
    with pytest.raises(httpx.ConnectError) as plain:                                  # what the researchers saw before
        trusting_root_only.get(site["url"])
    assert tls.incomplete_chain(plain.value)
    final, body, ctype = web._download(site["url"], trusting_root_only)
    text, title = web._text_of(body, ctype, final)
    assert "8.75 percent" in text and title == "Policy rate"


def test_verification_is_not_relaxed_when_the_chain_does_not_reach_a_trusted_root(site, monkeypatch):
    import certifi

    monkeypatch.setattr(tls, "CAFILE", certifi.where())                               # the test root is not among the real ones
    tls._contexts.clear()
    client = httpx.Client(verify=ssl.create_default_context(cafile=site["root"]))
    with pytest.raises(web.FetchError) as e:
        web._download(site["url"], client)
    assert "could not reach the site" in str(e.value)


def test_other_connection_failures_are_not_mistaken_for_a_missing_intermediate():
    assert not tls.incomplete_chain(httpx.ConnectTimeout("slow"))
    assert not tls.incomplete_chain(None)
