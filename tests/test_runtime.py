"""The run manager, against a real (throwaway) database and a scripted model.

Each test is one promise the production design makes: the agent narrates; it asks and
carries on; transient failures heal themselves; hard failures stop honestly; a crash is
picked up; the user can steer and stop.
"""

from __future__ import annotations

import httpx
import pytest
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from chui_reporter.agent import tools as T
from chui_reporter.agent.graph import build_agent
from chui_reporter.runtime import state
from chui_reporter.runtime.runner import Runtime
from tests.fakes import ScriptedChat, call


class _Fail(Exception):
    def __init__(self, msg, status_code):
        super().__init__(msg)
        self.status_code = status_code


def _finish_tools(rs):
    """Stand-ins for writing + rendering + inspecting, so a run can legitimately complete."""
    @tool
    def write_it(key: str, text: str) -> str:
        """Write a section."""
        rs.set_section(key, "Overview", text, 101)
        return "saved"

    @tool
    def render_it() -> str:
        """Render."""
        rs.mark("rendered_at")
        return "rendered 1 sections, 0 tables"

    @tool
    def look_it() -> str:
        """Inspect."""
        rs.mark("inspected_at")
        return "looked"

    return [write_it, render_it, look_it, T.ask_user, T.request_sources]


def _runtime(store, script, **kw):
    rs = {"s": None}
    saver = InMemorySaver()
    llm = ScriptedChat(script=script, seen=[])

    def factory(provider):
        rs["s"] = rt.report_store()
        return build_agent(saver, llm=llm, tools=_finish_tools(rs["s"]))

    rt = Runtime(store, saver=saver, agent_factory=factory, prepare=False, snapshot=False,
                 sleep=lambda s: None, **kw)
    return rt, llm


HAPPY = [call("write_it", {"key": "1.1", "text": "In Q2 2026, Chui Ventures deployed capital."}, "a"),
         call("render_it", {}, "b"), call("look_it", {}, "c")]


def _events(store, kind=None):
    ev = state.recent_events(store, 500)
    return [e for e in ev if kind is None or e["kind"] == kind]


def test_a_run_narrates_each_step_and_completes(store):
    rt, _ = _runtime(store, list(HAPPY))
    rid = rt._create("build", "Build it")
    assert rt.execute(rid) == "done"
    steps = [e["label"] for e in _events(store, "step")]
    assert len(steps) == 3 and steps[0].startswith("Running write it") or "Writing" in steps[0]
    assert len(_events(store, "step.done")) == 3
    assert state.get_run(store, rid)["status"] == "done"
    assert _events(store, "run.end")[-1]["detail"]["status"] == "done"


def test_the_agent_asks_waits_and_continues_with_the_answer(store):
    rt, llm = _runtime(store, [call("ask_user", {"question": "Which fair value is authoritative?",
                                                 "why_it_matters": "Sets 2.1.",
                                                 "options": ["Fund Model", "Valuation reports"]}, "q")] + HAPPY)
    rid = rt._create("build", "Build it")
    assert rt.execute(rid) == "waiting_user"
    q = state.open_questions(store)[0]
    assert q["options"] == ["Fund Model", "Valuation reports"] and q["status"] == "open"
    assert state.get_run(store, rid)["status"] == "waiting_user"

    rt.answer(q["id"], "Fund Model")                       # re-queues the run
    assert state.get_run(store, rid)["status"] == "queued"
    assert rt.execute(rid) == "done"
    seen = " ".join(str(m.content) for batch in llm.seen for m in batch)
    assert "The user answered: Fund Model" in seen


def test_requested_sources_resume_by_themselves_when_files_arrive(store):
    from chui_reporter.workspace import sync
    rt, _ = _runtime(store, [call("request_sources", {"slots": ["macro"], "why_it_matters": "3.1 needs it"}, "m")]
                     + HAPPY)
    rid = rt._create("build", "Build it")
    assert rt.execute(rid) == "waiting_data"

    import hashlib
    data = b"gdp,3.1"
    h = hashlib.sha256(data).hexdigest()
    sync.put_file(store, "Macro and Context/indicators.csv", data, h)
    ch = sync.commit(store, [{"path": "Macro and Context/indicators.csv", "sha256": h}])
    rt.on_sync(ch)                                          # the arrival is what answers it
    assert state.get_run(store, rid)["status"] == "queued"
    assert rt.execute(rid) == "done"
    assert not state.open_questions(store)


def test_a_transient_failure_is_retried_from_the_checkpoint(store):
    rt, llm = _runtime(store, [httpx.ConnectError("reset by peer")] + HAPPY)
    rid = rt._create("build", "Build it")
    assert rt.execute(rid) == "done"
    retry = _events(store, "retry")
    assert len(retry) == 1 and "connection" in retry[0]["label"].lower()
    assert retry[0]["detail"]["attempt"] == 1


def test_rate_limits_back_off_and_honour_the_attempt_budget(store):
    rt, _ = _runtime(store, [_Fail("slow down", 429)] * 20)
    rid = rt._create("build", "Build it")
    assert rt.execute(rid) == "failed"
    assert len(_events(store, "retry")) == 8
    assert "rate-limiting" in state.get_run(store, rid)["error"]


def test_a_hard_failure_stops_at_once_and_says_what_to_do(store):
    rt, _ = _runtime(store, [_Fail("invalid api key", 401)])
    rid = rt._create("build", "Build it")
    assert rt.execute(rid) == "failed"
    assert not _events(store, "retry")
    err = state.get_run(store, rid)["error"]
    assert "API key" in err and "saved" in err


def test_an_empty_balance_is_reported_as_such(store):
    rt, _ = _runtime(store, [_Fail("Insufficient Balance", 402)])
    rid = rt._create("build", "Build it")
    rt.execute(rid)
    assert "out of credit" in state.get_run(store, rid)["error"]


def test_a_crashed_run_is_found_and_resumed(store):
    rt, _ = _runtime(store, list(HAPPY))
    rid = rt._create("build", "Build it")
    # A previous process claimed it, then died: running, heartbeat long gone.
    with store.conn() as c:
        c.execute(f"UPDATE {store._t('run')} SET status='running', worker='dead:1', attempts=1, "
                  f"heartbeat_at=now() - interval '10 minutes' WHERE id=%s", (rid,))
    assert rt.recover() == 1
    assert state.get_run(store, rid)["status"] == "queued"
    assert _events(store, "recovered")
    assert rt.execute(rid) == "done"


def test_a_live_run_is_not_stolen(store):
    rt, _ = _runtime(store, list(HAPPY))
    rid = rt._create("build", "Build it")
    with store.conn() as c:
        c.execute(f"UPDATE {store._t('run')} SET status='running', worker='other:1', heartbeat_at=now() WHERE id=%s", (rid,))
    assert rt.recover() == 0
    assert state.get_run(store, rid)["status"] == "running"


def test_a_model_that_stops_early_is_sent_back_to_work(store):
    rt, llm = _runtime(store, [call("write_it", {"key": "1.1", "text": "In Q2 2026, Chui Ventures deployed."}, "a")]
                       + [__import__("langchain_core.messages", fromlist=["AIMessage"]).AIMessage(content="All done!")]
                       + [call("render_it", {}, "b"), call("look_it", {}, "c")])
    rid = rt._create("build", "Build it")
    assert rt.execute(rid) == "done"
    assert _events(store, "nudge")


def test_steering_is_applied_at_the_next_step(store):
    rt, llm = _runtime(store, list(HAPPY) + [call("render_it", {}, "d"), call("look_it", {}, "e")])
    rid = rt._create("build", "Build it")
    rt.steer("Keep the overview to two sentences")
    out = rt.execute(rid)
    assert out == "done", state.get_run(store, rid)["error"]
    seen = " ".join(str(m.content) for batch in llm.seen for m in batch)
    assert "two sentences" in seen
    assert _events(store, "steer")


def test_stopping_ends_the_run_at_the_next_step(store):
    rt, _ = _runtime(store, list(HAPPY))
    rid = rt._create("build", "Build it")
    orig = rt._narrate

    def stop_after_first(*a, **k):
        rt.stop()                                           # the user presses Stop mid-run
        return orig(*a, **k)

    rt._narrate = stop_after_first
    assert rt.execute(rid) == "stopped"
    assert state.get_run(store, rid)["status"] == "stopped"
    assert len(_events(store, "step")) == 1                 # it did not run on to the end


def test_repeating_the_same_call_is_interrupted_with_guidance(store):
    rt, llm = _runtime(store, [call("render_it", {}, f"r{i}") for i in range(3)] + list(HAPPY))
    rid = rt._create("build", "Build it")
    assert rt.execute(rid) == "done"
    seen = " ".join(str(m.content) for batch in llm.seen for m in batch)
    assert "same arguments" in seen


def test_a_message_that_arrives_after_the_last_step_is_still_delivered(store):
    """The agent can finish its turn between two of the user's keystrokes. A steer queued then used to
    be dropped silently when the run ended."""
    rt, llm = _runtime(store, list(HAPPY) + [call("render_it", {}, "d"), call("look_it", {}, "e")])
    rid = rt._create("build", "Build it")
    real = rt._segment
    sent = {"done": False}

    def segment(*a, **k):
        out = real(*a, **k)
        if not sent["done"] and out[0] == "finished":
            sent["done"] = True
            rt.steer("One more thing: keep it short.")          # arrives just after the agent finished
        return out

    rt._segment = segment
    assert rt.execute(rid) == "done"
    seen = " ".join(str(m.content) for batch in llm.seen for m in batch)
    assert "keep it short" in seen
    assert [e["detail"]["id"] for e in _events(store, "steer.applied")] == [_events(store, "steer")[0]["detail"]["id"]]


def test_a_queued_message_is_reported_if_the_run_ends_without_delivering_it(store):
    rt, _ = _runtime(store, [_Fail("bad key", 401)])
    rid = rt._create("build", "Build it")
    rt._steer.append({"id": "zz", "text": "late note"})
    assert rt.execute(rid) == "failed"
    assert [e["detail"]["id"] for e in _events(store, "steer.dropped")] == ["zz"]
