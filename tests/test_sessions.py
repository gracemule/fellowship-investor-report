"""Sessions: a new conversation with the agent over the same report, and earlier ones still readable."""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from chui_reporter.agent.graph import build_agent
from chui_reporter.runtime import state
from chui_reporter.runtime.runner import Runtime
from tests.fakes import ScriptedChat, call
from tests.test_runtime import _finish_tools


def _rt(store, script=()):
    saver = InMemorySaver()
    llm = ScriptedChat(script=list(script), seen=[])
    holder = {}

    def factory(provider):
        return build_agent(saver, llm=llm, tools=_finish_tools(holder["rt"].report_store()))

    rt = Runtime(store, saver=saver, agent_factory=factory, prepare=False, snapshot=False, sleep=lambda s: None)
    holder["rt"] = rt
    state.set_session_provider(lambda: rt.session()["id"])
    return rt, llm


@pytest.fixture(autouse=True)
def _reset_provider():
    yield
    state.set_session_provider(None)


def test_the_first_session_adopts_the_conversation_that_existed_before_sessions(store):
    rt, _ = _rt(store)
    s = rt.session()
    assert s["thread_id"] == f"default-{rt.period().code}", "work already done stays attached"
    assert rt.session()["id"] == s["id"], "asking again does not create another"


def test_a_new_session_has_its_own_thread_and_the_old_one_stays_readable(store):
    rt, _ = _rt(store)
    old = rt.session()
    state.emit(store, "steer", "tighten 1.2")                 # something said in the old session
    new = rt.new_session()
    state.emit(store, "steer", "now do the cover")
    assert new["id"] != old["id"] and new["thread_id"] != old["thread_id"]
    in_old = [e["label"] for e in state.recent_events(store, 50, session=old["id"], include_unassigned=True)]
    in_new = [e["label"] for e in state.recent_events(store, 50, session=new["id"])]
    assert "tighten 1.2" in in_old and "now do the cover" not in in_old
    assert "now do the cover" in in_new and "tighten 1.2" not in in_new
    assert "New session" in in_new


def test_events_from_before_sessions_belong_to_the_oldest_session(store):
    state.emit(store, "steer", "from long ago")                # no session provider yet
    rt, _ = _rt(store)
    oldest = rt.session()["id"]
    assert state.oldest_session_id(store) == oldest
    got = [e["label"] for e in state.recent_events(store, 50, session=oldest, include_unassigned=True)]
    assert "from long ago" in got
    rt.new_session()
    assert "from long ago" not in [e["label"] for e in state.recent_events(store, 50, session=rt.session()["id"])]


def test_titles_say_what_was_asked_and_runs_are_counted(store):
    rt, _ = _rt(store)
    rid = rt._create("build", "Build it")
    state.emit(store, "run.start", "Build it", run_id=rid, detail={"kind": "build"})
    state.update_run(store, rid, status="done")
    first = rt.session()["id"]
    rt.new_session()
    state.emit(store, "steer", "Please shorten the overview")
    by_id = {s["id"]: s for s in state.sessions(store)}
    assert by_id[first]["title"] == "Built the report" and by_id[first]["runs"] == 1
    cur = by_id[rt.session()["id"]]
    assert cur["title"] == "Please shorten the overview" and cur["runs"] == 0


def test_a_new_session_is_refused_while_the_agent_is_working(store):
    rt, _ = _rt(store)
    rt._create("build", "Build it")
    with pytest.raises(RuntimeError, match="busy"):
        rt.new_session()


def test_a_new_session_starts_a_clean_conversation_but_knows_the_report_exists(store):
    rt, llm = _rt(store, [call("write_it", {"key": "1.1", "text": "In Q2 2026, the Fund grew."}, "a"),
                          call("render_it", {}, "b"), call("look_it", {}, "c")])
    rid = rt._create("build", "Build it")
    assert rt.execute(rid) == "done"
    rs = rt.report_store()
    from chui_reporter.runtime import versions
    rs.set_meta(last_render={"pdf": "/nonexistent"})          # no real render in this test: record a version by hand
    state.save_version(rs, 1, {"hashes": {}}, b"%PDF", None, None)
    rt.new_session()
    llm.script, llm.i = [call("look_it", {}, "d")], 0
    rid2 = rt._create("steer", "Make 1.1 shorter")
    rt.execute(rid2)
    seen = " ".join(str(m.content) for m in llm.seen[-1])
    assert "already exists (version 1)" in seen and "report_outline" in seen
    assert "Build it" not in seen, "the new session does not carry the old conversation"
