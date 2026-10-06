"""Context compaction: what is trimmed, what is never touched, and that the conversation stays valid."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chui_reporter.runtime.compaction import KEEP_RECENT, STUB_CHARS, compact, estimate_tokens


def _exchange(i: int, size: int, tool="web_fetch"):
    cid = f"c{i}"
    return [AIMessage(content="", tool_calls=[{"name": tool, "args": {"url": f"https://x/{i}"}, "id": cid, "type": "tool_call"}]),
            ToolMessage(content=f"page {i} " + "x" * size, tool_call_id=cid, name=tool)]


def _thread(n: int, size: int):
    msgs = [HumanMessage(content="Build the report")]
    for i in range(n):
        msgs += _exchange(i, size, "web_search" if i % 2 else "web_fetch")
    return msgs


def test_a_conversation_under_the_budget_is_left_exactly_as_it_is():
    msgs = _thread(4, 100)
    view, stats = compact(msgs, budget=50_000)
    assert view == msgs and stats == {}


def test_stage_one_shortens_old_tool_results_but_removes_no_message():
    msgs = _thread(60, 4000)
    before = estimate_tokens(msgs)
    view, stats = compact(msgs, budget=before // 2)
    assert stats["stage"] == 1 and len(view) == len(msgs) and stats["messages_before"] == stats["messages_after"]
    assert stats["after"] < stats["before"]
    old = [m for m in view[:-KEEP_RECENT] if isinstance(m, ToolMessage)]
    assert all(len(str(m.content)) < STUB_CHARS + 120 for m in old) and "shortened" in str(old[0].content)
    assert stats["shortened"] == len([m for m in msgs[:-KEEP_RECENT] if isinstance(m, ToolMessage)])
    assert set(stats["by_tool"]) == {"web_fetch", "web_search"} and sum(stats["by_tool"].values()) == stats["shortened"]


def test_the_most_recent_messages_are_never_touched():
    msgs = _thread(60, 4000)
    view, _ = compact(msgs, budget=estimate_tokens(msgs) // 2)
    assert view[-KEEP_RECENT:] == msgs[-KEEP_RECENT:]
    assert view[0] == msgs[0], "the original instruction is kept"


def test_the_originals_are_not_modified_so_the_saved_history_stays_complete():
    msgs = _thread(60, 4000)
    snapshot = [str(m.content) for m in msgs]
    compact(msgs, budget=estimate_tokens(msgs) // 2)
    assert [str(m.content) for m in msgs] == snapshot


def test_stage_two_drops_whole_exchanges_never_a_call_without_its_result():
    msgs = _thread(80, 6000)
    view, stats = compact(msgs, budget=3000, brief=lambda: "Where the work stands: 12 facts.")
    assert stats["stage"] == 2 and len(view) < len(msgs) and stats["dropped"] > 0
    assert "Where the work stands: 12 facts." in str(view[1].content) and "condensed" in str(view[1].content).lower()
    answered = {m.tool_call_id for m in view if isinstance(m, ToolMessage)}
    called = {tc["id"] for m in view if isinstance(m, AIMessage) for tc in m.tool_calls}
    assert called == answered, "every call that remains has its result, and every result its call"
    assert not isinstance(view[2], ToolMessage), "the kept part never starts mid-exchange"
