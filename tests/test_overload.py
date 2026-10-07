"""A small server must not be run out of memory by the agent's own heavy work, and a Stop must always work.

On the 512 MB server the report build once pinned memory at its limit for half an hour: two builders opened the big workbooks side
by side, nothing answered (not even Stop), and the run looked alive. These tests hold the lines that prevent that."""

from __future__ import annotations

import threading
import time

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from openpyxl import Workbook as XlWorkbook

from chui_reporter.extract import workbook as wbmod
from chui_reporter.extract.workbook import ExtractionError, Workbook
from chui_reporter.runtime import runner, state, view
from chui_reporter.runtime.runner import Runtime


@pytest.fixture(autouse=True)
def _fresh_cache():
    wbmod.release_all()
    yield
    wbmod.release_all()


def _xlsx(path, rows=3):
    wb = XlWorkbook()
    ws = wb.active
    ws.title = "Sheet1"
    for i in range(rows):
        ws.append([f"label {i}", i])
    wb.save(path)
    return path


# ---- workbooks are shared, bounded, and refused when they would not fit ------------------------------------------------------


def test_the_same_unchanged_workbook_is_opened_once_and_shared(tmp_path):
    p = _xlsx(tmp_path / "a.xlsx")
    first = Workbook.open(p)
    assert Workbook.open(p) is first
    first.close()                                   # a shared workbook is other callers' too
    assert Workbook.open(p) is first and first.sheet("Sheet1")
    _xlsx(p, rows=5)                                # the file changed: a new one is read, never the stale copy
    assert Workbook.open(p) is not first


def test_the_least_recently_used_workbook_is_let_go_of_to_stay_inside_the_budget(tmp_path, monkeypatch):
    a, b, c = (_xlsx(tmp_path / f"{n}.xlsx") for n in "abc")
    size = a.stat().st_size
    monkeypatch.setattr(wbmod, "_BUDGET", size * wbmod._PER_BYTE * 2 + 1)            # room for two
    wa, wb_ = Workbook.open(a), Workbook.open(b)
    assert Workbook.open(a) is wa                                                    # a is now the more recent
    wc = Workbook.open(c)                                                            # so b goes
    assert Workbook.open(a) is wa and Workbook.open(c) is wc and Workbook.open(b) is not wb_
    wbmod.release_all()
    assert Workbook.open(a) is not wa


def test_a_workbook_is_refused_with_a_clear_error_when_the_server_cannot_hold_it(tmp_path, monkeypatch):
    p = _xlsx(tmp_path / "big.xlsx")
    monkeypatch.setattr(wbmod, "headroom", lambda: 1_000)                            # a server with nothing left
    with pytest.raises(ExtractionError, match="not enough memory"):
        Workbook.open(p)
    monkeypatch.setattr(wbmod, "headroom", lambda: None)                             # one that does not say: carry on
    assert Workbook.open(p).sheet("Sheet1")


def test_headroom_is_read_from_the_container_and_unknown_elsewhere():
    h = wbmod.headroom()
    assert h is None or isinstance(h, int)


# ---- heavy builders never run side by side -------------------------------------------------------------------------------------


def test_heavy_builders_run_one_at_a_time_and_let_go_of_what_they_opened(store, monkeypatch):
    from chui_reporter.agent import tools as T

    T.set_store(store)
    live, peak, released = [0], [0], []
    monkeypatch.setattr(T, "release_all", lambda: released.append(1))
    monkeypatch.setattr(T.B, "release_caches", lambda: released.append(2))

    def builder(_store):
        live[0] += 1
        peak[0] = max(peak[0], live[0])
        time.sleep(0.15)
        live[0] -= 1
        return "built"

    threads = [threading.Thread(target=lambda: T._heavy(builder, builder)) for _ in range(3)]     # three tool calls in one step
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert peak[0] == 1, "two builders were open at once"
    assert released.count(1) == 3 and released.count(2) == 3
    assert T._heavy(lambda s: (_ for _ in ()).throw(RuntimeError("boom"))).startswith("ERROR building"), "a failing builder is reported, not raised"


# ---- Stop works, even when the step in progress does not end -----------------------------------------------------------------


@pytest.fixture()
def rt(store):
    r = Runtime(store, saver=InMemorySaver(), agent_factory=lambda p: None, prepare=False, snapshot=False, sleep=lambda s: None)
    state.set_session_provider(lambda: r.session()["id"])
    yield r
    state.set_session_provider(None)


def _running(store, rt):
    rid = rt._create("build", "x")
    state.update_run(store, rid, status="running")
    return rid


def test_a_stop_is_recorded_at_once_and_a_step_that_never_ends_is_let_go_of(store, rt, monkeypatch):
    monkeypatch.setattr(runner, "STOP_GRACE", 0.2)
    rid = _running(store, rt)
    assert rt.stop() == {"ok": True, "when": "after_current_step"}
    assert state.get_run(store, rid)["stop_requested_at"], "remembered, so a restart does not bring the run back"
    assert any(e["kind"] == "run.stopping" for e in state.recent_events(store, 20))
    ends = []
    for _ in range(50):
        ends = [e for e in state.recent_events(store, 20) if e["kind"] == "run.end"]
        if ends:                                                       # (the feed line is written just after the status)
            break
        time.sleep(0.1)
    run = state.get_run(store, rid)
    assert run["status"] == "stopped" and rid in rt._abandoned
    assert ends and ends[-1]["detail"].get("forced") is True
    assert state.active_run(store) is None, "the page is free again"


def test_a_stop_that_the_agent_honours_in_time_is_not_forced(store, rt, monkeypatch):
    monkeypatch.setattr(runner, "STOP_GRACE", 3600)                    # the timer never fires here; the guard is called directly
    rid = _running(store, rt)
    rt.stop()
    state.update_run(store, rid, status="stopped")                    # the agent stopped by itself, as it does between steps
    rt._force_stop(rid)
    assert rid not in rt._abandoned and [e for e in state.recent_events(store, 20) if e["kind"] == "run.end"] == []
    rt._stop_run.clear()
    state.update_run(store, rid, status="running")                    # and a run that is working, with no stop asked, is left alone
    rt._force_stop(rid)
    assert state.get_run(store, rid)["status"] == "running" and rid not in rt._abandoned


def test_a_run_the_user_stopped_is_not_resumed_by_a_restart_but_an_interrupted_one_is(store, rt):
    stopped, killed = _running(store, rt), None
    state.request_stop(store, stopped)
    with store.conn() as c:
        c.execute(f"UPDATE {store._t('run')} SET heartbeat_at = now() - interval '10 minutes' WHERE id=%s", (stopped,))
    assert rt.recover() == 0 and state.get_run(store, stopped)["status"] == "stopped"
    state.update_run(store, stopped, status="done")
    killed = _running(store, rt)
    with store.conn() as c:
        c.execute(f"UPDATE {store._t('run')} SET heartbeat_at = now() - interval '10 minutes' WHERE id=%s", (killed,))
    assert rt.recover() == 1 and state.get_run(store, killed)["status"] == "queued", "an interrupted run still carries on"


def test_the_page_says_when_it_is_stopping_and_when_a_step_has_gone_quiet(store, rt, monkeypatch):
    rid = _running(store, rt)
    state.emit(store, "step", "Reading the fund model", run_id=rid)
    s = rt.snapshot_state()["status"]
    assert s["headline"] == "Working on the report" and "Still on this step" not in s["detail"]
    with store.conn() as c:
        c.execute(f"UPDATE {store._t('event')} SET created_at = now() - interval '12 minutes'")
    s = rt.snapshot_state()["status"]
    assert "Still on this step after 12 minutes" in s["detail"], s
    monkeypatch.setattr(runner, "STOP_GRACE", 3600)
    rt.stop()
    s = rt.snapshot_state()["status"]
    assert s["headline"] == "Stopping" and s["phase"] == "working" and "Finishing the step" in s["detail"]
    rt._stop_run.clear()
    assert view.STALLED_AFTER == 300


# ---- every tool that opens documents takes its turn ----------------------------------------------------------------------------


def test_every_tool_that_opens_documents_takes_turns_not_just_the_builders():
    from chui_reporter.agent import tools as T

    names = {t.name: t for t in T.ALL_TOOLS}
    assert T._TAKE_TURNS <= set(names)
    for n in T._TAKE_TURNS:
        assert hasattr(names[n].func, "__wrapped__"), f"{n} can still run alongside another"
    for light in ("list_sources", "report_set_section", "delegate_research", "ask_user", "research_macro"):
        assert not hasattr(names[light].func, "__wrapped__"), f"{light} does not open documents and must not wait for them"


def test_tools_wrapped_to_take_turns_never_overlap_and_let_go_when_asked(monkeypatch):
    from chui_reporter.agent import tools as T

    live, peak, freed = [0], [0], []
    monkeypatch.setattr(T, "release_all", lambda: freed.append(1))
    monkeypatch.setattr(T.B, "release_caches", lambda: freed.append(2))

    def work():
        live[0] += 1
        peak[0] = max(peak[0], live[0])
        time.sleep(0.1)
        live[0] -= 1
        return "ok"

    plain, big = T._taking_turns(work), T._taking_turns(work, let_go=True)
    threads = [threading.Thread(target=f) for f in (plain, big, plain, big)]       # four tools asked for in one step
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert peak[0] == 1 and sorted(freed) == [1, 1, 2, 2], "only the big readers let go"


# ---- a run left by a killed process is found shortly after the restart, and a run that keeps dying is not resumed for ever ------


def _age_heartbeat(store, rid, seconds):
    with store.conn() as c:
        c.execute(f"UPDATE {store._t('run')} SET heartbeat_at = now() - make_interval(secs => %s) WHERE id=%s", (seconds, rid))


def test_a_run_whose_process_was_killed_is_picked_up_after_the_restart_not_an_hour_later(store, rt):
    rid = _running(store, rt)
    _age_heartbeat(store, rid, 60)                       # the restart came within a minute: it does not yet look dead
    assert rt.recover() == 0 and state.get_run(store, rid)["status"] == "running"
    _age_heartbeat(store, rid, 100)                      # ... but a little later it plainly is
    rt._recover_again = [time.time() - 1, time.time() + 3600]
    rt._recover_if_due()
    assert state.get_run(store, rid)["status"] == "queued", "resumed from where it stopped"
    assert len(rt._recover_again) == 1, "each look is made once"
    rt._recover_if_due()
    assert len(rt._recover_again) == 1, "and not before its time"


def test_a_run_that_keeps_being_killed_is_handed_back_to_the_user_instead_of_looping(store, rt):
    rid = _running(store, rt)
    state.update_run(store, rid, attempts=runner.MAX_ATTEMPTS)
    _age_heartbeat(store, rid, 600)
    assert rt.recover() == 0
    run = state.get_run(store, rid)
    assert run["status"] == "failed" and "running out of memory" in run["error"]
    assert any(e["kind"] == "run.end" and e["detail"].get("kind") == "crash_loop" for e in state.recent_events(store, 20))
