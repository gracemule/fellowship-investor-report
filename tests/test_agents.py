"""Subagents: bulky work done in a context of its own, with its results checked by code before the report sees them."""

from __future__ import annotations

import json

import httpx
import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from chui_reporter import period as pr
from chui_reporter.agents import engine, macro, records
from chui_reporter.agents.engine import Context, Subagent, budgeted, extract_json, parse_result, run_subagent
from chui_reporter.agents.research import record_claims
from chui_reporter.services import web
from tests.fakes import RoutedChat, ScriptedChat, call

PAGE_KE = ("Central Bank of Kenya. The Monetary Policy Committee decided to maintain the Central Bank Rate (CBR) at 8.75 percent. "
           "Headline inflation was 6.4 percent in June 2026. The shilling traded at 129.63 per US dollar on 25 June 2026. " * 3)
QUOTE_RATE = "decided to maintain the Central Bank Rate (CBR) at 8.75 percent"
QUOTE_INFL = "Headline inflation was 6.4 percent in June 2026"
USAGE = {"input_tokens": 12_000, "output_tokens": 300, "total_tokens": 12_300, "input_token_details": {"cache_read": 11_000}}


def _claim(key, label, value, unit, as_of, url, quote):
    return {"key": key, "label": label, "value": value, "unit": unit, "as_of": as_of, "source_url": url, "quote": quote}


def _json(claims=(), gaps=(), summary="done"):
    return AIMessage(content="```json\n" + json.dumps({"summary": summary, "claims": list(claims), "gaps": list(gaps)}) + "\n```",
                     usage_metadata=USAGE)


def _fetch_tool(store, pages: dict[str, str]):
    @tool
    def web_fetch(url: str, find: str = "") -> str:
        """Read a page."""
        if url not in pages:
            return "ERROR: could not reach the site"
        web.save_snapshot(store, url, pages[url], "page", "direct")
        return pages[url][:4000]

    return web_fetch


class Feed:
    def __init__(self):
        self.events = []

    def emit(self, kind, label="", chapter=None, detail=None, **kw):
        self.events.append({"kind": kind, "label": label, "detail": detail or {}})

    def of(self, kind):
        return [e for e in self.events if e["kind"] == kind]


@pytest.fixture()
def ctx(store):
    store.ensure_report("Chui Ventures Fund I", "Q2 2026")
    feed, used = Feed(), []
    c = Context(store=store, report_id=store.report_id, run_id="run1", emit=feed.emit, tally=used.append,
                sleep=lambda s: None, llm_factory=lambda: None)
    c.feed, c.used = feed, used
    return c


def _spec(store, pages=None, **kw):
    return Subagent(name="web_researcher", title="Researcher", prompt="You research.",
                    tools=(_fetch_tool(store, pages or {"https://cbk.example/mpc": PAGE_KE}),), **kw)


def test_a_subagent_does_the_reading_and_hands_back_checked_json(store, ctx):
    ctx.llm_factory = lambda: ScriptedChat(script=[
        call("web_fetch", {"url": "https://cbk.example/mpc"}, "f1", usage=USAGE),
        _json([_claim("policy_rate", "Kenya policy rate", 8.75, "percent", "Jun 2026", "https://cbk.example/mpc", QUOTE_RATE)])], seen=[])
    out = run_subagent(_spec(store), "Find Kenya's policy rate.", label="Kenya", ctx=ctx)
    assert out.status == "done" and out.result.claims[0].value == 8.75
    assert out.usage["calls"] == 2 and out.usage["input"] == 24_000 and out.usage["cached"] == 22_000
    kinds = [e["kind"] for e in ctx.feed.events]
    assert kinds[0] == "subagent.start" and kinds[-1] == "subagent.done" and "step" in kinds and "step.done" in kinds
    assert all(e["detail"].get("sub") == out.id for e in ctx.feed.of("step")), "its steps are tagged so the feed can nest them"
    assert len(ctx.used) == 2, "every reply's tokens were passed to the run's counters"
    with store.conn() as c:
        row = c.execute(f"select status, usage, result from {store._t('subagent_run')} where id=%s", (out.id,)).fetchone()
    assert row["status"] == "done" and row["result"]["claims"][0]["key"] == "policy_rate"


def test_what_the_subagent_read_never_enters_the_callers_conversation(store, ctx):
    """The point of delegating: the main agent's context holds the verified figures, not the pages."""
    ctx.llm_factory = lambda: ScriptedChat(script=[call("web_fetch", {"url": "https://cbk.example/mpc"}, "f1"), _json()], seen=[])
    llm = ctx.llm_factory()
    ctx.llm_factory = lambda: llm
    run_subagent(_spec(store), "Find it.", label="Kenya", ctx=ctx)
    assert any("Central Bank Rate (CBR)" in str(m.content) for batch in llm.seen for m in batch), "the subagent itself did read it"


def test_a_tool_budget_stops_runaway_searching_and_says_what_to_do():
    calls = []

    @tool
    def web_search(query: str) -> str:
        """Search."""
        calls.append(query)
        return "results"

    b = budgeted(web_search, 2)
    assert b.invoke({"query": "a"}) == "results" and b.invoke({"query": "b"}) == "results"
    over = b.invoke({"query": "c"})
    assert over.startswith("ERROR") and "all 2 web_search calls" in over and "gaps" in over and len(calls) == 2


def test_a_reply_that_is_not_the_required_json_gets_one_chance_to_be_fixed(store, ctx):
    ctx.llm_factory = lambda: ScriptedChat(script=[AIMessage(content="Kenya's rate is 8.75%, I think."),
                                                    _json([_claim("policy_rate", "Kenya policy rate", 8.75, "percent", "Jun 2026", "u", "q")])], seen=[])
    out = run_subagent(_spec(store), "Find it.", label="Kenya", ctx=ctx)
    assert out.status == "done" and out.result.claims
    ctx.llm_factory = lambda: ScriptedChat(script=[AIMessage(content="no"), AIMessage(content="still no")], seen=[])
    bad = run_subagent(_spec(store), "Find it.", label="Kenya", ctx=ctx)
    assert bad.status == "failed" and "usable result" in bad.error


def test_a_transient_failure_is_retried_and_a_hard_one_ends_only_that_subagent(store, ctx):
    ctx.llm_factory = lambda: ScriptedChat(script=[httpx.ConnectError("reset"), _json()], seen=[])
    ok = run_subagent(_spec(store), "Find it.", label="Kenya", ctx=ctx)
    assert ok.status == "done" and ctx.feed.of("retry") and ctx.feed.of("retry")[0]["detail"]["sub"] == ok.id

    class Auth(Exception):
        status_code = 401

    ctx.llm_factory = lambda: ScriptedChat(script=[Auth("bad key")], seen=[])
    bad = run_subagent(_spec(store), "Find it.", label="Kenya", ctx=ctx)
    assert bad.status == "failed" and "API key" in bad.error


def test_stopping_the_run_stops_the_subagent(store, ctx):
    ctx.should_stop = lambda: True
    ctx.llm_factory = lambda: ScriptedChat(script=[call("web_fetch", {"url": "https://cbk.example/mpc"}, "f1"), _json()], seen=[])
    assert run_subagent(_spec(store), "Find it.", label="Kenya", ctx=ctx).status == "stopped"


def test_json_is_found_in_a_fenced_block_or_bare_and_bad_shapes_are_refused():
    assert extract_json('Here you go:\n```json\n{"a": 1}\n```')["a"] == 1
    assert extract_json('{"a": 2} trailing')["a"] == 2
    with pytest.raises(ValueError):
        parse_result('{"claims": [{"key": "x"}]}')
    with pytest.raises(ValueError):
        parse_result("no json here")


# -- checking claims before they reach the ledger ----------------------------------------------------


def _saved(store):
    web.save_snapshot(store, "https://cbk.example/mpc", PAGE_KE, "page", "direct")


def test_only_claims_whose_quotation_is_on_the_page_become_facts(store):
    _saved(store)
    res = parse_result(json.dumps({"claims": [
        _claim("policy_rate", "Kenya policy rate", 8.75, "percent", "Jun 2026", "https://cbk.example/mpc", QUOTE_RATE),
        _claim("inflation", "Kenya inflation", 6.9, "percent", "Jun 2026", "https://cbk.example/mpc", QUOTE_INFL),       # wrong number
        _claim("fx_usd", "Kenya fx", 129.63, "KES per USD", "25 Jun 2026", "https://cbk.example/mpc", "the shilling closed at 129.63 on Friday"),  # invented quote
    ], "gaps": [{"key": "gdp_growth", "reason": "page not found"}]}))
    rec = record_claims(store, res, group="test")
    assert set(rec["verified"]) == {"policy_rate"}
    assert set(rec["gaps"]) == {"gdp_growth", "inflation", "fx_usd"} and "could not be verified" in rec["gaps"]["inflation"]
    statuses = {r["label"]: r["status"] for r in store.find_facts("Kenya", limit=20)}
    assert statuses["Kenya policy rate (Jun 2026)"] == "extracted" and statuses["Kenya inflation (Jun 2026)"] == "claimed"
    assert 8.75 in store.grounded_values() and 6.9 not in store.grounded_values()


# -- the macro snapshot, end to end ------------------------------------------------------------------


def _country_script(country, url, page, claims, gaps=()):
    return [call("web_fetch", {"url": url}, f"f-{country}", usage=USAGE), _json(claims, gaps)]


def _macro_ctx(store, ctx, routes):
    chat = RoutedChat(routes=routes, pos={}, seen=[])
    ctx.llm_factory = lambda: chat
    ctx.parallel = 3
    return chat


def _patch_fetch(monkeypatch, store, pages):
    from chui_reporter.agents import research

    monkeypatch.setattr(research, "RESEARCHER", Subagent(name="web_researcher", title="Researcher", prompt="x",
                                                          tools=(_fetch_tool(store, pages),), budgets={}))


def test_research_macro_runs_countries_in_parallel_and_returns_a_compact_account(store, ctx, monkeypatch):
    pages = {"https://cbk.example/mpc": PAGE_KE, "https://cbn.example/mpr": "The MPC held the Monetary Policy Rate at 26.5 percent in May 2026. " * 5}
    _patch_fetch(monkeypatch, store, pages)
    _macro_ctx(store, ctx, {
        "Kenya": _country_script("Kenya", "https://cbk.example/mpc", PAGE_KE, [
            _claim("policy_rate", "Kenya policy rate", 8.75, "percent", "Jun 2026", "https://cbk.example/mpc", QUOTE_RATE),
            _claim("inflation", "Kenya inflation", 6.4, "percent", "Jun 2026", "https://cbk.example/mpc", QUOTE_INFL)],
            [{"key": "gdp_growth", "reason": "KNBS site could not be read"}]),
        "Nigeria": _country_script("Nigeria", "https://cbn.example/mpr", "", [
            _claim("policy_rate", "Nigeria policy rate", 26.5, "percent", "May 2026", "https://cbn.example/mpr",
                   "held the Monetary Policy Rate at 26.5 percent in May 2026")]),
        "Zambia": [call("web_fetch", {"url": "https://nowhere.example"}, "f-Z", usage=USAGE), _json([], [{"key": "inflation", "reason": "no page"}])],
    })
    text = macro.research_macro(ctx, ["Kenya", "Nigeria", "Zambia"], pr.Period.parse("2026Q2"))
    assert "Kenya: policy rate 8.75% (Jun 2026); inflation 6.4% (Jun 2026) | gaps: gdp growth: KNBS site could not be read" in text
    assert "Nigeria: policy rate 26.5% (May 2026)" in text and "Zambia: no verified figures" in text
    assert "none of it is in this conversation" in text and len(text) < 1600, "a page, not a transcript"
    assert len(ctx.feed.of("subagent.start")) == 3 and len(ctx.feed.of("subagent.done")) == 3
    assert {8.75, 6.4, 26.5} <= store.grounded_values()

    out = macro.build_table(store, store.report_id, ["Kenya", "Nigeria", "Zambia"])
    t = store.tables()["t_macro"]
    assert t["columns"][0] == "Country" and t["section_key"] == "3.1"
    rows = {r[0]: r for r in t["rows"]}
    assert rows["Kenya"][2:4] == ["6.4% (Jun 2026)", "8.75% (Jun 2026)"] and rows["Kenya"][1] == "—"
    assert rows["Zambia"][1:] == ["—", "—", "—", "—"] and "2 of 12" not in out


def test_one_countrys_failure_is_a_line_in_the_account_not_a_failed_run(store, ctx, monkeypatch):
    class Auth(Exception):
        status_code = 401

    _patch_fetch(monkeypatch, store, {"https://cbk.example/mpc": PAGE_KE})
    _macro_ctx(store, ctx, {
        "Kenya": _country_script("Kenya", "https://cbk.example/mpc", PAGE_KE, [
            _claim("policy_rate", "Kenya policy rate", 8.75, "percent", "Jun 2026", "https://cbk.example/mpc", QUOTE_RATE)]),
        "Ghana": [Auth("bad key")]})
    text = macro.research_macro(ctx, ["Kenya", "Ghana"], pr.Period.parse("2026Q2"))
    assert "Kenya: policy rate 8.75%" in text and "Ghana: NOT RESEARCHED" in text


def test_the_task_names_the_period_the_indicators_and_where_to_start():
    t = macro.task_for("Kenya", pr.Period.parse("2026Q2"))
    assert "30 June 2026" in t and all(k in t for k in macro.INDICATORS) and "centralbank.go.ke" in t
    assert "BCEAO" in macro.task_for("Senegal", pr.Period.parse("2026Q2"))
    assert "Find the central bank" in macro.task_for("Nowhereland", pr.Period.parse("2026Q2"))


def test_the_main_agent_delegates_and_cannot_search_the_web_itself():
    from chui_reporter.agent import tools as T

    names = {t.name for t in T.ALL_TOOLS}
    assert {"research_macro", "build_macro_table", "delegate_research"} <= names
    assert "web_search" not in names and "web_fetch" not in names


def test_delegate_research_returns_verified_findings_and_what_was_not_found(store, ctx, monkeypatch):
    from chui_reporter.agent import tools as T

    T.set_store(store)
    _patch_fetch(monkeypatch, store, {"https://cbk.example/mpc": PAGE_KE})
    _macro_ctx(store, ctx, {"policy rate": _country_script("x", "https://cbk.example/mpc", PAGE_KE, [
        _claim("policy_rate", "Kenya policy rate", 8.75, "percent", "Jun 2026", "https://cbk.example/mpc", QUOTE_RATE)],
        [{"key": "gdp", "reason": "not published yet"}])})
    engine.set_context(ctx)
    try:
        out = T.delegate_research.invoke({"question": "What was Kenya's policy rate in June 2026, in percent?"})
        assert "Kenya policy rate (Jun 2026) = 8.75 percent" in out and "NOT FOUND: gdp" in out and "none is in this conversation" in out
        assert T.delegate_research.invoke({"question": "rate?"}).startswith("ERROR")
    finally:
        engine.set_context(None)


def test_work_in_flight_when_a_process_dies_is_marked_interrupted_not_left_running(store, ctx):
    records.start(store, "s1", ctx, "web_researcher", "Kenya", "task")
    assert records.interrupt_running(store, 0) == 1
    with store.conn() as c:
        assert c.execute(f"select status from {store._t('subagent_run')} where id='s1'").fetchone()["status"] == "interrupted"


def test_through_the_runner_the_main_conversation_holds_the_figures_not_the_pages(store, monkeypatch):
    """The whole point, end to end: the main agent calls research_macro; the researchers read the pages in their own
    contexts; the main agent's conversation contains the verified account and nothing of the pages."""
    from langgraph.checkpoint.memory import InMemorySaver

    from chui_reporter.agent import llm as llm_mod
    from chui_reporter.agent import tools as T
    from chui_reporter.agent.graph import build_agent
    from chui_reporter.agents import research
    from chui_reporter.runtime import state
    from chui_reporter.runtime.runner import Runtime

    pages = {"https://cbk.example/mpc": PAGE_KE}
    monkeypatch.setattr(research, "RESEARCHER", Subagent(name="web_researcher", title="Researcher", prompt="x",
                                                          tools=(_fetch_tool(store, pages),), budgets={}))
    sub_llm = RoutedChat(routes={"Kenya": _country_script("Kenya", "https://cbk.example/mpc", PAGE_KE, [
        _claim("policy_rate", "Kenya policy rate", 8.75, "percent", "Jun 2026", "https://cbk.example/mpc", QUOTE_RATE)])},
        pos={}, seen=[])
    monkeypatch.setattr(llm_mod, "get_llm", lambda *a, **k: sub_llm)

    saver = InMemorySaver()
    main_llm = ScriptedChat(script=[call("research_macro", {"countries": ["Kenya"]}, "m1"),
                                    call("build_macro_table", {"countries": ["Kenya"]}, "m2")], seen=[])
    store.ensure_report("Chui Ventures Fund I", "Q2 2026")

    def factory(provider):
        return build_agent(saver, llm=main_llm, tools=[T.research_macro, T.build_macro_table])

    rt = Runtime(store, saver=saver, agent_factory=factory, prepare=False, snapshot=False, sleep=lambda s: None, max_nudges=0)
    rid = rt._create("steer", "Add the macro snapshot")
    rt.execute(rid)

    main_text = " ".join(str(m.content) for batch in main_llm.seen for m in batch)
    assert "Kenya: policy rate 8.75% (Jun 2026)" in main_text and "none of it is in this conversation" in main_text
    assert "Central Bank Rate (CBR)" not in main_text and "Monetary Policy Committee decided" not in main_text, "no page text leaked"
    sub_text = " ".join(str(m.content) for batch in sub_llm.seen for m in batch)
    assert "Monetary Policy Committee decided" in sub_text, "the researcher did read it"

    ev = state.recent_events(store, 200)
    assert [e["kind"] for e in ev if e["kind"].startswith("subagent")] == ["subagent.start", "subagent.done"]
    nested = [e for e in ev if e["kind"] == "step" and e["detail"].get("sub")]
    assert nested and all(e["run_id"] == rid for e in nested)
    usage = state.get_run(store, rid)["usage"]
    assert usage["sub"]["calls"] == 2 and usage["sub"]["input"] == 24_000, "researcher tokens are counted apart"
    assert rt.report_store().tables()["t_macro"]["rows"][0][3] == "8.75% (Jun 2026)"


# -- the quarter a figure belongs to ---------------------------------------------------------------------


@pytest.mark.parametrize("as_of,expected", [
    ("Jun 2026", False), ("June 2026", False), ("Q2 2026", False), ("Q1 2026", False), ("2026-06", False), ("2025", False),
    ("Jul 2026", True), ("July 2026", True), ("Aug 2026", True), ("Q3 2026", True), ("2026-09", True), ("Q1 2027", True),
    ("latest", False),
    # a label that mentions the quarter in passing must not hide the later month it is really about
    ("August 2026 (latest month published; Q2 2026 ended June 2026)", True),
    ("Q1 2026 (published after Jul 2026)", True),
    ("Q2 2026 average (period average, not period end)", False)])
def test_a_figure_for_a_period_after_the_quarter_is_recognised(as_of, expected):
    from datetime import date

    from chui_reporter.agents.research import after

    assert after(as_of, date(2026, 6, 30)) is expected


def test_a_claim_for_a_later_period_is_never_recorded_even_if_it_is_true(store):
    from datetime import date

    web.save_snapshot(store, "https://nbs.example/cpi", "Headline inflation rose to 15.39 percent in August 2026. " * 5, "p", "direct")
    res = parse_result(json.dumps({"claims": [_claim("inflation", "Nigeria inflation", 15.39, "percent", "Aug 2026",
                                                     "https://nbs.example/cpi", "Headline inflation rose to 15.39 percent in August 2026.")]}))
    rec = record_claims(store, res, group="t", quarter_end=date(2026, 6, 30))
    assert rec["verified"] == {} and "after the quarter" in rec["gaps"]["inflation"]
    assert 15.39 not in store.grounded_values()


def test_as_of_must_be_a_short_period_so_explanations_go_in_basis():
    with pytest.raises(ValueError):
        parse_result(json.dumps({"claims": [_claim("inflation", "x", 1.0, "percent",
                                                   "August 2026 (latest month published; Q2 ended June)", "u", "q")]}))
    ok = parse_result(json.dumps({"claims": [dict(_claim("inflation", "x", 1.0, "percent", "Jun 2026", "u", "q"), basis="year on year")]}))
    assert ok.claims[0].basis == "year on year"


def test_the_table_keeps_a_figure_an_earlier_run_verified_when_a_later_run_found_less(store, ctx, monkeypatch):
    from chui_reporter.agents.research import research_one

    pages = {"https://cbk.example/mpc": PAGE_KE}
    _patch_fetch(monkeypatch, store, pages)
    full = _country_script("Kenya", "https://cbk.example/mpc", PAGE_KE, [
        _claim("policy_rate", "Kenya policy rate", 8.75, "percent", "Jun 2026", "https://cbk.example/mpc", QUOTE_RATE),
        _claim("inflation", "Kenya inflation", 6.4, "percent", "Jun 2026", "https://cbk.example/mpc", QUOTE_INFL)])
    thin = [call("web_fetch", {"url": "https://cbk.example/mpc"}, "f2"), _json([], [{"key": "policy_rate", "reason": "not found this time"}])]
    task = macro.task_for("Kenya", pr.Period.parse("2026Q2"))
    _macro_ctx(store, ctx, {"Kenya": full})
    research_one(ctx, task, label="Kenya", group="t")
    _macro_ctx(store, ctx, {"Kenya": thin})
    research_one(ctx, task, label="Kenya", group="t")
    macro.build_table(store, store.report_id, ["Kenya"])
    row = store.tables()["t_macro"]["rows"][0]
    assert row[2:4] == ["6.4% (Jun 2026)", "8.75% (Jun 2026)"], "the newer, thinner run did not erase what was verified before"


def test_the_researcher_can_check_a_figure_for_free_before_reporting_it(store, ctx):
    from chui_reporter.agents.research import verify_figure

    web.save_snapshot(store, "https://cbk.example/mpc", PAGE_KE, "p", "direct")
    engine.set_context(ctx)
    try:
        assert verify_figure.invoke({"source_url": "https://cbk.example/mpc", "quote": QUOTE_RATE, "value": 8.75}).startswith("OK")
        bad = verify_figure.invoke({"source_url": "https://cbk.example/mpc", "quote": QUOTE_RATE, "value": 9.5})
        assert bad.startswith("NOT VERIFIED")
        assert "not been fetched" in verify_figure.invoke({"source_url": "https://other", "quote": QUOTE_RATE, "value": 8.75})
    finally:
        engine.set_context(None)


def test_researchers_can_be_sent_back_for_only_the_missing_figures(store, ctx, monkeypatch):
    from chui_reporter.agents.research import research_one

    _patch_fetch(monkeypatch, store, {"https://cbk.example/mpc": PAGE_KE})
    task = macro.task_for("Kenya", pr.Period.parse("2026Q2"), ["fx_usd"])
    assert "fx_usd" in task and "policy_rate:" not in task and "inflation:" not in task
    assert "policy_rate:" in macro.task_for("Kenya", pr.Period.parse("2026Q2"))
    first = _country_script("Kenya", "https://cbk.example/mpc", PAGE_KE, [
        _claim("policy_rate", "Kenya policy rate", 8.75, "percent", "Jun 2026", "https://cbk.example/mpc", QUOTE_RATE)])
    _macro_ctx(store, ctx, {"Kenya": first})
    research_one(ctx, macro.task_for("Kenya", pr.Period.parse("2026Q2")), label="Kenya", group="t")
    second = _country_script("Kenya", "https://cbk.example/mpc", PAGE_KE, [
        _claim("inflation", "Kenya inflation", 6.4, "percent", "Jun 2026", "https://cbk.example/mpc", QUOTE_INFL)])
    _macro_ctx(store, ctx, {"Kenya": second})
    research_one(ctx, task, label="Kenya", group="t")
    macro.build_table(store, store.report_id, ["Kenya"])
    assert store.tables()["t_macro"]["rows"][0][2:4] == ["6.4% (Jun 2026)", "8.75% (Jun 2026)"], "both passes are in one table"
    assert "unknown indicator" in macro.research_macro(ctx, ["Kenya"], pr.Period.parse("2026Q2"), ["gdp"])


def test_a_long_period_note_from_an_older_run_does_not_widen_the_table_cell(store):
    from chui_reporter.agents import records

    sid = "sub1"
    records.start(store, sid, type("C", (), {"report_id": store.report_id, "run_id": None, "session_id": None})(), "web_researcher", "Kenya", "t")
    records.finish(store, sid, "done", {"recorded": {"verified": {"fx_usd": {
        "value": 129.63, "unit": "KES", "as_of": "June 2026 monthly average (BCEAO Bulletin Mensuel des Statistiques - Juin 2026)"}}}}, {}, None)
    macro.build_table(store, store.report_id, ["Kenya"])
    assert store.tables()["t_macro"]["rows"][0][4] == "129.63 (June 2026 monthly average)"


def test_a_closed_door_is_not_knocked_on_twice_and_costs_no_budget():
    from langchain_core.tools import tool

    seen = []

    @tool
    def web_fetch(url: str) -> str:
        """fetch"""
        seen.append(url)
        return "ERROR: the site answered 403" if "blocked.example" in url else f"page {url}"

    guarded = engine.remembering_dead_ends(engine.budgeted(web_fetch, 4))
    assert guarded.invoke({"url": "https://blocked.example/a"}).startswith("ERROR: the site answered 403")
    assert "already tried" in guarded.invoke({"url": "https://blocked.example/a"})        # the same address
    assert guarded.invoke({"url": "https://blocked.example/b"}).startswith("ERROR: the site answered 403")
    assert "not worth more of your budget" in guarded.invoke({"url": "https://blocked.example/c"})    # the site, after two refusals
    assert seen == ["https://blocked.example/a", "https://blocked.example/b"], "refused calls never reach the network"
    assert guarded.invoke({"url": "https://open.example/x"}) == "page https://open.example/x"
    assert guarded.invoke({"url": "https://open.example/y"}) == "page https://open.example/y"
    # four calls reached the tool (a, b, x, y); the refused ones did not use the budget of 4
    assert guarded.invoke({"url": "https://open.example/z"}).startswith("ERROR: you have used all 4")


def test_the_macro_gap_notes_are_derived_from_the_ledger_and_replace_stale_ones(store, ctx, monkeypatch):
    from chui_reporter.agents.research import research_one

    store.ensure_report("Chui Ventures Fund I", "Q2 2026")
    store.add_review_note("Macro", "Kenya GDP growth is omitted because KNBS was unreachable (an older, hand-written note).", "info")
    store.add_review_note("Layout", "The Macroeconomic indicators table is clipped at the right margin.", "warning")
    store.add_review_note("Funds", "The two sources disagree on 12 companies.", "warning")
    _patch_fetch(monkeypatch, store, {"https://cbk.example/mpc": PAGE_KE})
    script = _country_script("Kenya", "https://cbk.example/mpc", PAGE_KE, [
        _claim("policy_rate", "Kenya policy rate", 8.75, "percent", "Jun 2026", "https://cbk.example/mpc", QUOTE_RATE)],
        gaps=[{"key": "gdp_growth", "reason": "KNBS answered 403"}])
    _macro_ctx(store, ctx, {"Kenya": script})
    research_one(ctx, macro.task_for("Kenya", pr.Period.parse("2026Q2")), label="Kenya", group="t")
    msg = macro.build_table(store, store.report_id, ["Kenya"])
    assert "Kenya: policy rate 8.75% (Jun 2026)" in msg, "the prose is written from the table's own cells"
    notes = {n["text"]: n["area"] for n in store.review_notes()}
    assert any(t.startswith("Kenya: gdp growth is a dash") and "KNBS answered 403" in t for t in notes)
    assert any(t.startswith("Kenya: inflation is a dash") for t in notes)
    assert not any("hand-written" in t or "clipped" in t for t in notes), "old macro notes and old complaints about the table are gone"
    assert any("12 companies" in t for t in notes), "notes about other things are untouched"
    macro.build_table(store, store.report_id, ["Kenya"])                       # rebuilding does not duplicate the notes
    assert len([n for n in store.review_notes() if n["area"] == "Macro snapshot"]) == 3


def test_members_of_a_monetary_union_share_the_currency_and_policy_rate_figures(store):
    from chui_reporter.agents import records

    class C:
        report_id = store.report_id
        run_id = session_id = None

    claim = lambda v, as_of: {"value": v, "unit": "percent", "as_of": as_of}
    records.start(store, "s1", C, "web_researcher", "Côte d'Ivoire", "t")
    records.finish(store, "s1", "done", {"recorded": {"verified": {"policy_rate": claim(3.0, "Jun 2026"),
                                                                    "fx_usd": {"value": 569.4, "unit": "XOF", "as_of": "Jun 2026"},
                                                                    "inflation": claim(1.8, "Q1 2026")}}}, {}, None)
    records.start(store, "s2", C, "web_researcher", "Senegal", "t")
    records.finish(store, "s2", "done", {"recorded": {"verified": {}, "gaps": {"fx_usd": "pages did not render"}}}, {}, None)
    records.start(store, "s3", C, "web_researcher", "Kenya", "t")
    records.finish(store, "s3", "done", {"recorded": {"verified": {}}}, {}, None)
    macro.build_table(store, store.report_id, ["Côte d'Ivoire", "Senegal", "Kenya"])
    rows = {r[0]: r for r in store.tables()["t_macro"]["rows"]}
    assert rows["Senegal"][3:5] == ["3.0% (Jun 2026)", "569.4 (Jun 2026)"]
    assert rows["Senegal"][2] == "—", "only the shared figures are shared: inflation is national"
    assert rows["Kenya"][3:5] == ["—", "—"], "a country outside the union borrows nothing"
    assert not any(n["text"].startswith("Senegal: exchange rate") for n in store.review_notes())
