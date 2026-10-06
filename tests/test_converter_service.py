"""The LibreOffice service and the client that talks to it, without LibreOffice: the conversion itself is replaced, so what
is tested is everything around it (who may ask, what is accepted, one at a time, what comes back, how the app copes with a
sleeping host)."""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from chui_reporter.converter import service
from chui_reporter.render import converters
from chui_reporter.render.convert import ConversionError

PDF = b"%PDF-1.7 fake"


def bundle(docx=("report.docx", b"docx bytes"), fonts=None, extra=()) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if docx:
            z.writestr(docx[0], docx[1])
        for n, d in (fonts or {}).items():
            z.writestr(f"fonts/{n}", d)
        for n, d in extra:
            z.writestr(n, d)
    return buf.getvalue()


@pytest.fixture()
def svc(monkeypatch, tmp_path):
    monkeypatch.setenv("CHUI_CONVERTER_TOKEN", "s3cret-token")
    monkeypatch.setattr(service, "FONT_DIR", tmp_path / "fonts")
    monkeypatch.setattr(service, "_last_selftest", 0.0)
    calls = []

    def fake_docx_to_pdf(src, out_dir, **kw):
        calls.append(src.name)
        out_dir.mkdir(parents=True, exist_ok=True)
        pdf = out_dir / (src.stem + ".pdf")
        pdf.write_bytes(PDF)
        return pdf

    monkeypatch.setattr(service, "docx_to_pdf", fake_docx_to_pdf)
    monkeypatch.setattr(service, "_stray_soffice", lambda: None)
    with TestClient(service.create_app()) as c:
        c.calls = calls
        yield c


AUTH = {"Authorization": "Bearer s3cret-token"}


def test_health_answers_get_and_head_without_doing_any_work(svc):
    assert svc.get("/healthz").status_code == 200
    assert svc.head("/healthz").status_code == 200
    assert svc.calls == []


def test_only_the_right_token_may_convert(svc):
    assert svc.post("/convert", content=bundle()).status_code == 401
    assert svc.post("/convert", content=bundle(), headers={"Authorization": "Bearer nope"}).status_code == 401
    assert svc.post("/convert", content=bundle(), headers={"Authorization": "s3cret-token"}).status_code == 401
    assert svc.calls == []
    ok = svc.post("/convert", content=bundle(), headers=AUTH)
    assert ok.status_code == 200 and ok.content == PDF and ok.headers["content-type"] == "application/pdf"


def test_a_service_with_no_token_set_refuses_everything(svc, monkeypatch):
    monkeypatch.delenv("CHUI_CONVERTER_TOKEN")
    assert svc.post("/convert", content=bundle(), headers=AUTH).status_code == 503
    assert svc.post("/convert", content=bundle(), headers={"Authorization": "Bearer "}).status_code == 503


def test_bad_requests_are_refused_before_any_conversion(svc):
    assert svc.post("/convert", content=b"not a zip", headers=AUTH).status_code == 400
    assert svc.post("/convert", content=bundle(docx=None), headers=AUTH).status_code == 400
    two = bundle(extra=[("second.docx", b"x")])
    assert svc.post("/convert", content=two, headers=AUTH).status_code == 400
    assert svc.calls == []


def test_fonts_are_installed_once_and_anything_else_is_ignored(svc):
    ttf = b"\x00\x01\x00\x00" + b"x" * 200
    r = svc.post("/convert", content=bundle(fonts={"Larken Regular.ttf": ttf, "evil.sh": b"#!/bin/sh", "fake.ttf": b"not a font"}),
                 headers=AUTH)
    assert r.status_code == 200
    assert sorted(p.name for p in service.FONT_DIR.iterdir()) == ["Larken Regular.ttf"]
    assert service.install_fonts({"Larken Regular.ttf": ttf}) == 0, "the same font is not installed again"
    assert service.install_fonts({"../../escape.ttf": ttf}) == 1 and not (service.FONT_DIR.parent.parent / "escape.ttf").exists()


def test_oversized_font_bundles_are_refused(svc, monkeypatch):
    monkeypatch.setattr(service, "MAX_FONTS", 100)
    r = svc.post("/convert", content=bundle(fonts={"big.ttf": b"\x00\x01\x00\x00" + b"x" * 500}), headers=AUTH)
    assert r.status_code == 400


def test_a_busy_service_says_when_to_come_back_instead_of_queueing_forever(svc, monkeypatch):
    monkeypatch.setattr(service, "QUEUE_WAIT", 0.05)
    with service._turn:                                   # another conversion is running
        r = svc.post("/convert", content=bundle(), headers=AUTH)
    assert r.status_code == 503 and r.headers["retry-after"] == "20"
    assert svc.post("/convert", content=bundle(), headers=AUTH).status_code == 200, "and it recovers"


def test_a_failed_conversion_is_reported_not_hidden(svc, monkeypatch):
    def boom(*a, **k):
        raise ConversionError("conversion produced no PDF")

    monkeypatch.setattr(service, "docx_to_pdf", boom)
    r = svc.post("/convert", content=bundle(), headers=AUTH)
    assert r.status_code == 500 and "no PDF" in r.json()["error"]
    assert service._turn.acquire(blocking=False), "the turn is released after a failure"
    service._turn.release()


def test_the_selftest_reports_time_and_memory_and_is_rate_limited(svc):
    r = svc.get("/selftest")
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True and "libreoffice_peak_mb" in body and "seconds" in body
    assert svc.get("/selftest").status_code == 429
    assert zipfile.is_zipfile(io.BytesIO(service._sample_docx())), "the built-in sample is a real zip package"


# ---- the app's side ---------------------------------------------------------------------------------------------------


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _conv(handler, fonts=True):
    c = converters.RemoteLibreOfficeConverter("https://conv.example/", "tok", client=_client(handler), sleep=lambda s: None)
    c.fonts = {f: b"\x00\x01\x00\x00" + f.encode() for f in converters.FAMILY_FILES} if fonts else {}
    return c


def test_the_remote_converter_sends_the_document_and_fonts_and_saves_the_pdf(tmp_path):
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"], seen["url"] = req.headers["authorization"], str(req.url)
        seen["names"] = sorted(zipfile.ZipFile(io.BytesIO(req.content)).namelist())
        return httpx.Response(200, content=PDF)

    docx = tmp_path / "Report.docx"
    docx.write_bytes(b"docx")
    out = _conv(handler).convert(docx, tmp_path / "out")
    assert out.read_bytes() == PDF and out.name == "Report.pdf"
    assert seen["auth"] == "Bearer tok" and seen["url"] == "https://conv.example/convert"
    assert seen["names"] == ["Report.docx", "fonts/Larken Bold.ttf", "fonts/Larken Italic.ttf", "fonts/Larken Regular.ttf"]


def test_a_sleeping_host_is_waited_for_and_a_wrong_token_is_not_retried(tmp_path):
    docx = tmp_path / "r.docx"
    docx.write_bytes(b"d")
    answers = iter([httpx.Response(503), httpx.ConnectError("asleep"), httpx.Response(502), httpx.Response(200, content=PDF)])

    def waking(req):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    assert _conv(waking).convert(docx, tmp_path / "o").read_bytes() == PDF
    hits = []

    def refusing(req):
        hits.append(1)
        return httpx.Response(401, json={"error": "not authorised"})

    with pytest.raises(ConversionError, match="rejected the token"):
        _conv(refusing).convert(docx, tmp_path / "o")
    assert len(hits) == 1


def test_a_host_that_never_wakes_gives_up_with_a_clear_message(tmp_path):
    docx = tmp_path / "r.docx"
    docx.write_bytes(b"d")
    n = []

    def down(req):
        n.append(1)
        return httpx.Response(503, text="busy")

    with pytest.raises(ConversionError, match="answered 503"):
        _conv(down).convert(docx, tmp_path / "o")
    assert len(n) == len(converters.RemoteLibreOfficeConverter.RETRY_WAIT) + 1


def test_preparing_the_remote_converter_checks_its_settings_and_fonts(tmp_path, monkeypatch):
    with pytest.raises(ConversionError, match="CHUI_CONVERTER_URL"):
        converters.RemoteLibreOfficeConverter("", "").prepare(tmp_path)
    fonts = tmp_path / "Branding" / "Fonts" / "Larken"
    fonts.mkdir(parents=True)
    monkeypatch.setattr(converters.Path, "home", classmethod(lambda cls: tmp_path / "nohome"))
    with pytest.raises(ConversionError, match="Larken font files are missing"):
        converters.RemoteLibreOfficeConverter("https://x", "t").prepare(tmp_path)
    for f in converters.FAMILY_FILES.values():
        (fonts / f).write_bytes(b"\x00\x01\x00\x00fake")
    c = converters.RemoteLibreOfficeConverter("https://x", "t")
    c.prepare(tmp_path)
    assert set(c.fonts) == set(converters.FAMILY_FILES)


def test_the_remote_converter_is_chosen_when_it_is_configured_and_nothing_local_exists(monkeypatch):
    monkeypatch.setattr(converters, "find_soffice", lambda: (_ for _ in ()).throw(ConversionError("none")))
    for k in ("CHUI_PDF_CONVERTER", "CHUI_CONVERTER_URL", "CHUI_CONVERTER_TOKEN", "ILOVEAPI_PUBLIC_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ConversionError, match="no PDF converter"):
        converters.get_converter()
    monkeypatch.setenv("CHUI_CONVERTER_URL", "https://conv.example")
    monkeypatch.setenv("CHUI_CONVERTER_TOKEN", "t")
    assert converters.get_converter().name == "remote"
    d = converters.describe()
    assert d["name"] == "remote" and d["ready"] is True
    monkeypatch.setenv("CHUI_PDF_CONVERTER", "remote")
    monkeypatch.delenv("CHUI_CONVERTER_TOKEN")
    assert converters.describe()["ready"] is False


def test_an_idle_production_server_does_not_poll_the_database_every_half_minute(monkeypatch):
    from chui_reporter.runtime.runner import janitor_every

    monkeypatch.delenv("CHUI_JANITOR_SECONDS", raising=False)
    monkeypatch.setenv("CHUI_ENV", "production")
    assert janitor_every() == 3600
    monkeypatch.setenv("CHUI_ENV", "dev")
    assert janitor_every() == 30
    monkeypatch.setenv("CHUI_JANITOR_SECONDS", "5")
    assert janitor_every() == 5
