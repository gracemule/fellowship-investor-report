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

    T.set_store(store)
    T.set_router(SearchRouter(store, {"tavily": Tavily(""), "brave": Brave("")}, ["tavily", "brave"]))
    out = T.web_search.invoke({"query": "kenya inflation"})
    assert out.startswith("ERROR") and "no API key set" in out and "request_sources" in out
    T.set_router(None)


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
