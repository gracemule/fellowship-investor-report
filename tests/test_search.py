"""Web search with fallback, and web figures held to the same standard as workbook figures.

Providers are exercised through httpx's mock transport: no network, no credit spent."""

from __future__ import annotations

import json

import httpx
import pytest

from chui_reporter.agent.store import Fact
from chui_reporter.services import usage, web
from chui_reporter.services.search import Brave, NoSearchAvailable, ProviderError, SearchRouter, Tavily


def _tavily(handler):
    return Tavily("tv-key", httpx.Client(transport=httpx.MockTransport(handler)))


def _brave(handler):
    return Brave("br-key", httpx.Client(transport=httpx.MockTransport(handler)))


def _ok_tavily(request):
    assert request.headers["authorization"] == "Bearer tv-key"
    return httpx.Response(200, json={"results": [{"title": "CBK", "url": "https://cbk.example/rate", "content": "Policy rate 9.75%"}]})


def _ok_brave(request):
    assert request.headers["x-subscription-token"] == "br-key"
    return httpx.Response(200, json={"web": {"results": [{"title": "KNBS", "url": "https://knbs.example", "description": "GDP grew 4.7%"}]}})


def _router(store, tav, brv, **kw):
    return SearchRouter(store, {"tavily": tav, "brave": brv}, ["tavily", "brave"], sleep=lambda s: None, **kw)


def test_tavily_is_the_default(store):
    r = _router(store, _tavily(_ok_tavily), _brave(_ok_brave)).search("kenya policy rate")
    assert r.provider == "tavily" and r.hits[0].title == "CBK"
    assert usage.used_this_month(store, "tavily") == 1 and usage.used_this_month(store, "brave") == 0


@pytest.mark.parametrize("status", [432, 433])
def test_spent_tavily_credit_moves_to_brave_and_is_remembered(store, status):
    calls = {"tavily": 0}

    def spent(request):
        calls["tavily"] += 1
        return httpx.Response(status, json={"detail": {"error": "limit"}})

    router = _router(store, _tavily(spent), _brave(_ok_brave))
    first = router.search("a")
    assert first.provider == "brave" and any("tavily" in n for n in first.notes)
    assert usage.get_state(store, "tavily")["state"] == "exhausted"
    router.search("b")
    assert calls["tavily"] == 1, "a provider known to be spent is not asked again during its cooldown"


def test_the_default_returns_by_itself_once_the_cooldown_is_over(store):
    usage.set_state(store, "tavily", "exhausted", hours=0.0001, detail="spent")
    with store.conn() as c:
        c.execute(f"UPDATE {store._t('service_state')} SET until = now() - interval '1 minute' WHERE provider='tavily'")
    assert _router(store, _tavily(_ok_tavily), _brave(_ok_brave)).search("x").provider == "tavily"


def test_a_rate_limit_is_retried_once_then_the_other_provider_is_used(store):
    seen = []

    def limited(request):
        seen.append(1)
        return httpx.Response(429, headers={"retry-after": "2"}, json={})

    r = _router(store, _tavily(limited), _brave(_ok_brave)).search("x")
    assert r.provider == "brave" and len(seen) == 2


def test_brave_429_with_no_monthly_allowance_left_counts_as_spent(store):
    def monthly(request):
        return httpx.Response(429, headers={"x-ratelimit-remaining": "1, 0", "x-ratelimit-limit": "1, 1000"}, json={})

    def tav_spent(request):
        return httpx.Response(432, json={})

    with pytest.raises(NoSearchAvailable) as e:
        _router(store, _tavily(tav_spent), _brave(monthly)).search("x")
    assert usage.get_state(store, "brave")["state"] == "exhausted"
    assert set(e.value.reasons) == {"tavily", "brave"}


def test_a_bad_key_or_a_server_error_falls_through_without_poisoning_the_provider(store):
    def server(request):
        return httpx.Response(503)

    r = _router(store, _tavily(server), _brave(_ok_brave)).search("x")
    assert r.provider == "brave" and usage.get_state(store, "tavily")["state"] == "ok"
    def bad_key(request):
        return httpx.Response(401)
    r2 = _router(store, _tavily(bad_key), _brave(_ok_brave)).search("y")
    assert r2.provider == "brave" and usage.get_state(store, "tavily")["state"] == "auth"


def test_with_no_keys_the_agent_is_told_plainly(store, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    from chui_reporter.agent import tools as T
    from chui_reporter.agent import web_tools as W

    T.set_store(store)
    W.set_router(SearchRouter(store, {"tavily": Tavily(""), "brave": Brave("")}, ["tavily", "brave"]))
    out = W.web_search.invoke({"query": "kenya inflation"})
    assert out.startswith("ERROR") and "no API key set" in out and "request_sources" in out
    W.set_router(None)


def test_status_shows_each_provider_for_the_interface(store):
    st = _router(store, _tavily(_ok_tavily), Brave("")).status()
    assert [(s["name"], s["configured"], s["state"]) for s in st] == [("tavily", True, "ok"), ("brave", False, "unset")]


# -- reading pages and checking web figures --------------------------------------------------


PAGE = """<html><head><title>Monetary Policy</title><style>.x{}</style></head><body>
<script>var secret = 1234;</script><h1>Central Bank Rate</h1>
<table><tr><td>Central Bank Rate</td><td>9.75%</td></tr></table>
<p>Inflation eased to 4.1 percent in September 2026.</p></body></html>"""


def test_html_becomes_readable_text_without_scripts_or_styles():
    text, title = web.html_to_text(PAGE)
    assert title == "Monetary Policy" and "Central Bank Rate | 9.75%" in text and "1234" not in text and ".x{}" not in text


@pytest.mark.parametrize("url", ["http://127.0.0.1/admin", "http://localhost:8000/", "http://169.254.169.254/latest/meta-data",
                                 "http://10.0.0.5/x", "file:///etc/passwd", "ftp://example.com/x", "http://user:pw@example.com/"])
def test_private_and_unsafe_addresses_are_refused(url):
    with pytest.raises(web.FetchError):
        web.check_url(url)


def test_a_redirect_into_the_internal_network_is_refused(store, monkeypatch):
    monkeypatch.setattr(web, "_check_host", lambda h: (_ for _ in ()).throw(web.FetchError("private")) if h == "internal.local" else None)

    def handler(request):
        if request.url.host == "good.example":
            return httpx.Response(302, headers={"location": "http://internal.local/secret"})
        return httpx.Response(200, text="secret")

    with pytest.raises(web.FetchError):
        web.fetch(store, "https://good.example/start", client=httpx.Client(transport=httpx.MockTransport(handler)))


def _fetch_page(store, monkeypatch, html=PAGE, url="https://cbk.example/rate"):
    monkeypatch.setattr(web, "_check_host", lambda h: None)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html, headers={"content-type": "text/html"})))
    return web.fetch(store, url, client=client)


def test_a_fetched_page_is_kept_as_evidence(store, monkeypatch):
    page = _fetch_page(store, monkeypatch)
    snap = web.get_snapshot(store, page["url"])
    assert "9.75%" in snap["text"] and len(snap["sha256"]) == 64


def test_a_web_figure_is_verified_by_a_quotation_that_is_really_on_the_page(store, monkeypatch):
    _fetch_page(store, monkeypatch)
    good = Fact("Kenya policy rate", 9.75, unit="percent", source_file="https://cbk.example/rate",
                source_cell="Central Bank Rate | 9.75%")
    assert web.verify_web_claim(good, store)[0]
    inflation = Fact("Kenya inflation", 4.1, unit="percent", source_file="https://cbk.example/rate",
                     source_cell="Inflation eased to 4.1 percent in September 2026.")
    assert web.verify_web_claim(inflation, store)[0]


def test_an_invented_quote_a_wrong_number_or_an_unfetched_page_is_not_verified(store, monkeypatch):
    _fetch_page(store, monkeypatch)
    base = dict(unit="percent", source_file="https://cbk.example/rate")
    assert not web.verify_web_claim(Fact("x", 9.75, source_cell="The rate is 9.75% per the bank", **base), store)[0]
    assert not web.verify_web_claim(Fact("x", 11.0, source_cell="Central Bank Rate | 9.75%", **base), store)[0]
    assert not web.verify_web_claim(Fact("x", 9.75, source_cell="9.75%", **base), store)[0], "a bare number is not a quotation"
    ok, why = web.verify_web_claim(Fact("x", 9.75, unit="percent", source_file="https://never.fetched/x",
                                        source_cell="Central Bank Rate | 9.75%"), store)
    assert not ok and "web_fetch" in why


def test_a_verified_web_figure_licenses_prose_through_the_normal_save_path(store, monkeypatch):
    from chui_reporter.agent import tools as T
    from chui_reporter.render.gate import check_grounded

    T.set_store(store)
    _fetch_page(store, monkeypatch)
    out = T.report_save_facts.invoke({"facts_json": json.dumps([{
        "label": "Kenya policy rate (Sep-26)", "value": 9.75, "unit": "percent", "as_of": "2026-09",
        "source_file": "https://cbk.example/rate", "source_cell": "Central Bank Rate | 9.75%"}])})
    assert "1 verified" in out
    assert check_grounded("The policy rate stood at 9.75% (Sep-26).", store.grounded_values()) == []
    assert check_grounded("The policy rate stood at 10.5%.", store.grounded_values()) == ["10.5%"]


# -- a page that is blocked to us is asked of a provider that can render it ---------------------


class _Extractor:
    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def extract(self, urls):
        self.asked.append(urls)
        return "tavily", {u: self.pages.get(u, "") for u in urls}


def _client(status, body="", ctype="text/html"):
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, text=body, headers={"content-type": ctype})))


def test_a_blocked_page_is_read_through_the_extractor(store, monkeypatch):
    monkeypatch.setattr(web, "_check_host", lambda h: None)
    url = "https://blocked.example/rates"
    ex = _Extractor({url: "Central Bank Rate | 9.75% " * 30})
    page = web.fetch(store, url, router=ex, client=_client(403))
    assert page["via"] == "tavily-extract" and ex.asked == [[url]]
    assert "9.75%" in web.get_snapshot(store, url)["text"]


def test_a_blocked_page_with_no_extractor_is_reported_not_invented(store, monkeypatch):
    monkeypatch.setattr(web, "_check_host", lambda h: None)
    with pytest.raises(web.FetchError, match="403"):
        web.fetch(store, "https://blocked.example/x", router=None, client=_client(403))


def test_an_empty_script_drawn_page_is_extracted_but_a_refused_address_never_is(store, monkeypatch):
    monkeypatch.setattr(web, "_check_host", lambda h: None)
    url = "https://spa.example/rates"
    ex = _Extractor({url: "Policy rate 9.75% " * 40})
    page = web.fetch(store, url, router=ex, client=_client(200, "<html><body><div id='app'></div><script>x()</script></body></html>"))
    assert page["via"] == "tavily-extract"
    monkeypatch.undo()
    ex2 = _Extractor({})
    with pytest.raises(web.Refused):
        web.fetch(store, "http://127.0.0.1/admin", router=ex2)
    assert ex2.asked == [], "an address we refuse is not handed to another service either"


def test_comma_decimals_are_recognised_in_a_quotation(store, monkeypatch):
    monkeypatch.setattr(web, "_check_host", lambda h: None)
    page = "<html><body><p>L'inflation en glissement annuel ressort à 0,8 % en juin 2026.</p></body></html>" * 20
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page, headers={"content-type": "text/html"})))
    web.fetch(store, "https://bceao.example/note", client=client)
    f = Fact("UEMOA inflation", 0.8, unit="percent", source_file="https://bceao.example/note",
             source_cell="L'inflation en glissement annuel ressort à 0,8 % en juin 2026.")
    assert web.verify_web_claim(f, store)[0]
    wrong = Fact("x", 0.9, unit="percent", source_file="https://bceao.example/note", source_cell=f.source_cell)
    assert not web.verify_web_claim(wrong, store)[0]


def test_section_pages_follow_the_documents_numbering_not_the_agents_order_values():
    """The macro section was given order 30 but prints between sections 2 and 4; its page was lost."""
    import io

    from pypdf import PdfWriter

    from chui_reporter.runtime.versions import section_pages

    class P:
        def __init__(self, t): self.t = t
        def extract_text(self): return self.t
    import pypdf
    texts = ["cover", "contents", "overview one", "capital activity two", "macro snapshot three", "balance sheet four"]
    orig = pypdf.PdfReader
    pypdf.PdfReader = lambda path: type("R", (), {"pages": [P(t) for t in texts]})()
    try:
        secs = [{"key": "1.1", "title": "Overview", "ord": 1}, {"key": "1.2", "title": "Capital Activity", "ord": 2},
                {"key": "3.1", "title": "Macro Snapshot", "ord": 30}, {"key": "4.1", "title": "Balance Sheet", "ord": 20}]
        assert section_pages("x.pdf", secs) == {"1.1": 3, "1.2": 4, "3.1": 5, "4.1": 6}
    finally:
        pypdf.PdfReader = orig


def test_a_social_media_post_is_not_a_source_for_a_figure(store, monkeypatch):
    monkeypatch.setattr(web, "_check_host", lambda h: None)
    page = "<html><body><p>Headline inflation rose to 15.91 percent in June 2026.</p></body></html>" * 20
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page, headers={"content-type": "text/html"})))
    for url in ("https://x.com/NBS_Nigeria/status/1", "https://www.facebook.com/nbs/posts/2", "https://m.youtube.com/watch?v=3"):
        web.fetch(store, url, client=client)
        f = Fact("Nigeria inflation", 15.91, unit="percent", source_file=url, source_cell="Headline inflation rose to 15.91 percent in June 2026.")
        ok, why = web.verify_web_claim(f, store)
        assert not ok and "social media" in why, url
    assert web.is_social("https://twitter.com/x") and not web.is_social("https://www.nbs.gov.ng/inflation")


def test_review_notes_about_a_filled_gap_can_be_removed_precisely(store):
    from chui_reporter.agent import tools as T

    T.set_store(store)
    store.ensure_report("F", "Q2 2026")
    store.add_review_note("3.1", "Nigeria inflation is omitted because the page could not be read.", "warning")
    store.add_review_note("3.1", "Kenya GDP growth is omitted because the site blocked us.", "warning")
    assert T.report_remove_review_note.invoke({"contains": "x"}).startswith("ERROR")
    assert "removed 1" in T.report_remove_review_note.invoke({"contains": "Nigeria inflation is omitted"})
    assert [n["text"][:10] for n in store.review_notes()] == ["Kenya GDP "]
    assert T.report_remove_review_note.invoke({"contains": "no such note anywhere"}).startswith("no review note")
