"""The agent's saved working memory stays small, without ever costing it the ability to resume.

These run the real Postgres checkpointer (in a scratch schema on a direct, non-pooled connection, so the library's
unqualified table names land there) and a real agent over it, because what matters is that a conversation still resumes
after its history is pruned, not just that rows disappear."""

from __future__ import annotations

import os
import re
import uuid

import psycopg
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row

from chui_reporter.agent.graph import build_agent
from chui_reporter.runtime import retention
from tests.fakes import ScriptedChat, call


@pytest.fixture()
def scratch():
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")
    direct = re.sub(r"-pooler(?=\.)", "", url)          # a session-level SET needs a direct connection
    schema = f"t_{uuid.uuid4().hex[:10]}"
    cx = psycopg.connect(direct, autocommit=True, prepare_threshold=None, row_factory=dict_row)
    cx.execute(f"CREATE SCHEMA {schema}")
    cx.execute(f"SET search_path TO {schema}")
    saver = PostgresSaver(cx)
    saver.setup()
    try:
        yield cx, saver
    finally:
        cx.execute("SET search_path TO public")
        cx.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        cx.close()


@tool
def bulky(text: str) -> str:
    """Return something large, as a web page or a workbook dump would."""
    return (text + " ") * 400


def _agent(saver, steps: int):
    llm = ScriptedChat(script=[call("bulky", {"text": f"step {i}"}, f"c{i}") for i in range(steps)] + [AIMessage(content="All done.")], seen=[])
    return build_agent(saver, llm=llm, tools=[bulky], breakpoints=False)


def _run(agent, thread: str, text: str = "go"):
    cfg = {"configurable": {"thread_id": thread}, "recursion_limit": 200}
    agent.invoke({"messages": [HumanMessage(content=text)]}, cfg)
    return cfg


def _count(cx, table, thread=None):
    q, a = f"SELECT count(*) AS n FROM {table}", ()
    if thread:
        q, a = q + " WHERE thread_id=%s", (thread,)
    return cx.execute(q, a).fetchone()["n"]


def test_pruning_keeps_the_newest_checkpoints_and_the_conversation_still_resumes(scratch):
    cx, saver = scratch
    agent = _agent(saver, 12)
    cfg = _run(agent, "t1")
    before_state = [(m.type, m.content) for m in agent.get_state(cfg).values["messages"]]
    n_cp, n_bl, n_wr = (_count(cx, t, "t1") for t in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"))
    assert n_cp > 20, "a 12-step run saves a checkpoint for every step"

    out = retention.prune_thread(cx, "t1", keep=2)
    assert out["checkpoints"] == n_cp - 2
    assert _count(cx, "checkpoints", "t1") == 2
    assert _count(cx, "checkpoint_blobs", "t1") < n_bl and _count(cx, "checkpoint_writes", "t1") < n_wr
    after_state = [(m.type, m.content) for m in agent.get_state(cfg).values["messages"]]
    assert after_state == before_state, "the conversation is exactly as it was"
    # every copy that remains is used by a remaining checkpoint, and every copy a remaining checkpoint uses is there
    assert cx.execute("""SELECT count(*) AS n FROM checkpoint_blobs b WHERE NOT EXISTS (
                           SELECT 1 FROM checkpoints c WHERE c.thread_id=b.thread_id AND c.checkpoint_ns=b.checkpoint_ns
                             AND c.checkpoint->'channel_versions'->>b.channel = b.version)""").fetchone()["n"] == 0

    again = ScriptedChat(script=[AIMessage(content="Picked up where we left off.")], seen=[])
    resumed = build_agent(saver, llm=again, tools=[bulky], breakpoints=False)
    resumed.invoke({"messages": [HumanMessage(content="and one more thing")]}, cfg)
    seen = [str(m.content)[:40] for m in again.seen[0]]
    assert any("step 11" in x for x in seen) and any("go" == x for x in seen), "it still remembers the whole conversation"


def test_pruning_one_conversation_leaves_every_other_alone(scratch):
    cx, saver = scratch
    agent = _agent(saver, 6)
    _run(agent, "a")
    _run(_agent(saver, 6), "b")
    b_before = [_count(cx, t, "b") for t in ("checkpoints", "checkpoint_blobs", "checkpoint_writes")]
    retention.prune_thread(cx, "a", keep=1)
    assert _count(cx, "checkpoints", "a") == 1
    assert [_count(cx, t, "b") for t in ("checkpoints", "checkpoint_blobs", "checkpoint_writes")] == b_before


def test_a_conversation_that_is_already_small_is_not_touched(scratch):
    cx, saver = scratch
    _run(_agent(saver, 0), "tiny")
    n = _count(cx, "checkpoints", "tiny")
    out = retention.prune_thread(cx, "tiny", keep=max(n, 2))
    assert out == {"checkpoints": 0, "writes": 0, "blobs": 0} and _count(cx, "checkpoints", "tiny") == n


def test_prune_all_skips_the_conversations_in_use(scratch):
    cx, saver = scratch
    for t in ("busy", "idle"):
        _run(_agent(saver, 5), t)
    assert sorted(retention.threads_over(cx, keep=2)) == ["busy", "idle"]
    deleted = retention.prune_all(cx, keep=2, skip={"busy"})
    assert deleted > 0 and _count(cx, "checkpoints", "idle") == 2 and _count(cx, "checkpoints", "busy") > 2


def test_compaction_reports_first_then_rebuilds_the_tables_and_every_conversation_still_loads(scratch):
    cx, saver = scratch
    agents = {t: _agent(saver, n) for t, n in (("long", 14), ("short", 4))}
    cfgs = {t: _run(a, t) for t, a in agents.items()}
    states = {t: [(m.type, m.content) for m in agents[t].get_state(cfgs[t]).values["messages"]] for t in agents}
    before = {t: _count(cx, t) for t in retention.TABLES}

    dry = retention.compact(cx, keep=2, apply=False)
    assert dry["applied"] is False and dry["rows_before"] == before and dry["rows_kept"]["checkpoints"] == 4
    assert {t: _count(cx, t) for t in retention.TABLES} == before, "a dry run changes nothing"

    done = retention.compact(cx, keep=2, apply=True)
    assert done["applied"] is True and done["rows_kept"]["checkpoints"] == 4
    assert _count(cx, "checkpoints") == 4 and _count(cx, "checkpoint_blobs") < before["checkpoint_blobs"]
    assert sum(done["after"].values()) < sum(done["before"].values()), "the space really comes back"
    for t in agents:
        assert [(m.type, m.content) for m in agents[t].get_state(cfgs[t]).values["messages"]] == states[t], f"{t} is intact"
    resumed = build_agent(saver, llm=ScriptedChat(script=[AIMessage(content="ok")], seen=[]), tools=[bulky], breakpoints=False)
    resumed.invoke({"messages": [HumanMessage(content="continue")]}, cfgs["long"])


def test_compaction_changes_nothing_if_it_fails_part_way(scratch, monkeypatch):
    cx, saver = scratch
    _run(_agent(saver, 3), "x")
    before = {t: _count(cx, t) for t in retention.TABLES}
    monkeypatch.setattr(retention, "table_sizes", lambda *a, **k: {t: 0 for t in retention.TABLES})
    real = retention._one

    def sabotage(cx_, sql, args=()):
        if "SELECT count(*) FROM checkpoint_blobs" == sql:           # the check after the rebuild
            return 10 ** 9
        return real(cx_, sql, args)

    monkeypatch.setattr(retention, "_one", sabotage)
    with pytest.raises(RuntimeError, match="do not match"):
        retention.compact(cx, keep=2, apply=True)
    monkeypatch.undo()
    assert {t: _count(cx, t) for t in retention.TABLES} == before, "the whole rebuild was rolled back"


def test_the_budget_net_drops_the_longest_idle_first_and_never_a_protected_conversation(scratch):
    cx, saver = scratch
    for t in ("oldest", "middle", "newest", "running"):
        _run(_agent(saver, 5), t)
    size = retention.payloads(cx)
    assert set(size) == {"oldest", "middle", "newest", "running"}
    under = retention.enforce_budget(cx, ["oldest", "middle", "running", "newest"], {"running"}, budget_mb=500)
    assert under == [], "below the budget nothing is dropped, however old"
    budget_mb = sum(size.values()) * 0.9 / 1048576
    dropped = retention.enforce_budget(cx, ["running", "oldest", "middle", "newest"], {"running", "newest"}, budget_mb=budget_mb)
    assert dropped and dropped[0] == "oldest" and "running" not in dropped and "newest" not in dropped
    assert _count(cx, "checkpoints", "running") > 0 and _count(cx, "checkpoints", "newest") > 0
    assert _count(cx, "checkpoints", "oldest") == 0 and _count(cx, "checkpoint_blobs", "oldest") == 0


def test_the_command_refuses_to_touch_the_tables_while_a_run_is_active(store, monkeypatch, capsys):
    from chui_reporter.runtime import state

    rid = state.create_run(store, "build", "x", "thread-x")
    state.update_run(store, rid, status="running")
    monkeypatch.setattr("chui_reporter.agent.store.Store", lambda *a, **k: store)
    assert retention.main(["--apply"]) == 2
    assert "active or waiting" in capsys.readouterr().err


# ---- the runtime's part ---------------------------------------------------------------------------------------------


def test_a_run_prunes_every_few_steps_and_again_when_it_ends(store, monkeypatch):
    from tests.test_runtime import HAPPY, _runtime

    rt, _ = _runtime(store, list(HAPPY))
    pruned = []
    rt._prune = lambda thread: pruned.append(thread)
    monkeypatch.setattr(retention, "PRUNE_EVERY", 2)
    rid = rt._create("build", "Build it")
    assert rt.execute(rid) == "done"
    thread = rt.thread_id()
    assert pruned and set(pruned) == {thread}, "pruned the run's own conversation"
    assert len(pruned) >= 2, "once between steps, once when the run ended"


def test_housekeeping_never_fails_a_run_and_is_a_no_op_without_a_database_pool(store):
    from tests.test_runtime import _runtime

    rt, _ = _runtime(store, [])
    assert rt._pool is None
    rt._prune("anything")
    rt._retention_pass()
