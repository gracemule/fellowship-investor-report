"""Context compaction: late, measured, frozen, and always a valid conversation."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chui_reporter.runtime.compaction import KEEP_RECENT, STUB_CHARS, Compactor, ContextMeter, estimate_tokens, make_hook


def _exchange(i: int, size: int, tool="web_fetch", usage=None, arg_size=0):
    cid = f"c{i}"
    return [AIMessage(content="", tool_calls=[{"name": tool, "args": {"url": f"https://x/{i}", "note": "n" * arg_size}, "id": cid, "type": "tool_call"}],
                      usage_metadata=usage),
            ToolMessage(content=f"page {i} " + "x" * size, tool_call_id=cid, name=tool)]


def _thread(n: int, size: int):
    msgs = [HumanMessage(content="Build the report")]
    for i in range(n):
        msgs += _exchange(i, size, "web_search" if i % 2 else "web_fetch")
    return msgs


def _meter(window=100_000, **kw):
    return ContextMeter(window, reserve=10_000, overhead=0, **kw)


def test_far_below_the_threshold_nothing_is_touched():
    msgs = _thread(10, 200)
    view, stats = Compactor(_meter(1_000_000)).view(msgs)
    assert view == msgs and stats == {}


def test_clearing_comes_first_removes_no_message_and_keeps_the_latest_untouched():
    msgs = _thread(60, 4000)
    m = _meter(window=int(estimate_tokens(msgs) * 1.4), fraction=0.5, discard_fraction=0.95)
    view, stats = Compactor(m).view(msgs)
    assert stats["stage"] == 1 and len(view) == len(msgs)
    old = [x for x in view[:-KEEP_RECENT] if isinstance(x, ToolMessage)]
    assert all(len(str(x.content)) < STUB_CHARS + 120 and "cleared" in str(x.content) for x in old)
    assert view[-KEEP_RECENT:] == msgs[-KEEP_RECENT:] and view[0] == msgs[0]
    assert stats["shortened"] == len(old) and set(stats["by_tool"]) == {"web_fetch", "web_search"}
    assert stats["after"] < stats["before"]


def test_the_originals_are_not_modified():
    msgs = _thread(60, 4000)
    snap = [str(x.content) for x in msgs]
    Compactor(_meter(window=int(estimate_tokens(msgs) * 1.4), fraction=0.5)).view(msgs)
    assert [str(x.content) for x in msgs] == snap


def test_the_boundary_is_frozen_so_the_prefix_stays_identical_between_calls():
    """The provider caches the start of the prompt; rewriting it every call would forfeit that."""
    msgs = _thread(60, 4000)
    c = Compactor(_meter(window=int(estimate_tokens(msgs) * 1.4), fraction=0.5, discard_fraction=0.99))
    v1, s1 = c.view(msgs)
    grown = msgs + _exchange(900, 4000) + _exchange(901, 4000)
    v2, s2 = c.view(grown)
    assert s1 and not s2, "no second event: nothing new had to be decided"
    n = len(msgs) - KEEP_RECENT
    assert [str(x.content) for x in v2[:n]] == [str(x.content) for x in v1[:n]], "the cleared prefix did not change"


def test_it_acts_again_only_when_the_conversation_has_grown_past_the_threshold_again():
    msgs = _thread(60, 4000)
    m = _meter(window=int(estimate_tokens(msgs) * 1.4), fraction=0.5, discard_fraction=0.99)
    c = Compactor(m)
    c.view(msgs)
    first = c.mask_upto
    big = msgs
    for i in range(40):
        big = big + _exchange(1000 + i, 4000)
    _, stats = c.view(big)
    assert stats and c.mask_upto > first, "the boundary moved forward in a new step, never back"


def _heavy_thread(n: int):
    """The bulk is in the agent's own calls, which clearing tool results cannot shrink."""
    msgs = [HumanMessage(content="Build the report")]
    for i in range(n):
        msgs += _exchange(i, 300, arg_size=6000)
    return msgs


def test_clearing_alone_is_enough_when_it_is_enough():
    msgs = _thread(80, 6000)
    m = _meter(window=int(estimate_tokens(msgs) * 1.1), fraction=0.2, discard_fraction=0.4)
    _, stats = Compactor(m, brief=lambda: "x").view(msgs)
    assert stats["stage"] == 1, "tool output was the bulk; nothing needed to be discarded"


def test_at_the_upper_threshold_old_history_is_dropped_whole_and_replaced_by_the_brief():
    msgs = _heavy_thread(80)
    m = _meter(window=int(estimate_tokens(msgs) * 1.1), fraction=0.2, discard_fraction=0.4)
    view, stats = Compactor(m, brief=lambda: "Where the work stands: 12 facts.").view(msgs)
    assert stats["stage"] == 2 and len(view) < len(msgs) and stats["dropped"] > 0
    assert view[0] == msgs[0] and "Where the work stands: 12 facts." in str(view[1].content)
    answered = {x.tool_call_id for x in view if isinstance(x, ToolMessage)}
    called = {tc["id"] for x in view if isinstance(x, AIMessage) for tc in x.tool_calls}
    assert called == answered and not isinstance(view[2], ToolMessage)


def test_the_brief_is_fixed_when_the_cut_is_made_so_later_calls_do_not_vary():
    msgs = _heavy_thread(80)
    seq = iter(["first brief", "second brief"])
    m = _meter(window=int(estimate_tokens(msgs) * 1.1), fraction=0.2, discard_fraction=0.4)
    c = Compactor(m, brief=lambda: next(seq))
    v1, _ = c.view(msgs)
    v2, _ = c.view(msgs + _exchange(500, 100, arg_size=10))
    assert "first brief" in str(v1[1].content) and "first brief" in str(v2[1].content)


def test_the_hook_returns_a_view_and_reports_only_real_decisions():
    msgs = _thread(60, 4000)
    events = []
    hook = make_hook(_meter(window=int(estimate_tokens(msgs) * 1.4), fraction=0.5, discard_fraction=0.99), on_compact=events.append)
    out = hook({"messages": msgs})["llm_input_messages"]
    hook({"messages": msgs})
    assert len(out) == len(msgs) and len(events) == 1
