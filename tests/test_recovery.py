"""Interrupted runs must stay usable. A tool call that never returned (the process was
killed mid-call) used to make the whole thread unusable: any new instruction was
rejected as an invalid history."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph

from chui_reporter.agent.run import repair_dangling_tool_calls


def _agent():
    g = StateGraph(MessagesState)
    g.add_node("agent", lambda s: {"messages": []})
    g.add_node("tools", lambda s: {"messages": []})
    g.add_edge(START, "agent")
    g.add_edge("agent", "tools")
    g.add_edge("tools", END)
    return g.compile(checkpointer=MemorySaver())


def _call(i, name="excel_dump_region"):
    return {"name": name, "args": {}, "id": f"call_{i}", "type": "tool_call"}


def test_dangling_tool_calls_are_given_an_interrupted_result():
    agent, cfg = _agent(), {"configurable": {"thread_id": "t"}}
    agent.update_state(cfg, {"messages": [
        HumanMessage(content="go"),
        AIMessage(content="", tool_calls=[_call(1), _call(2, "read_pdf")]),
        ToolMessage(content="ok", tool_call_id="call_1", name="excel_dump_region"),
    ]}, as_node="agent")
    assert repair_dangling_tool_calls(agent, cfg) == 1       # only call_2 was dangling
    msgs = agent.get_state(cfg).values["messages"]
    ids = {m.tool_call_id for m in msgs if isinstance(m, ToolMessage)}
    assert ids == {"call_1", "call_2"}
    fix = next(m for m in msgs if isinstance(m, ToolMessage) and m.tool_call_id == "call_2")
    assert "interrupted" in fix.content and fix.status == "error"


def test_a_healthy_thread_is_left_alone():
    agent, cfg = _agent(), {"configurable": {"thread_id": "t2"}}
    agent.update_state(cfg, {"messages": [
        HumanMessage(content="go"), AIMessage(content="", tool_calls=[_call(1)]),
        ToolMessage(content="ok", tool_call_id="call_1", name="excel_dump_region")]}, as_node="agent")
    assert repair_dangling_tool_calls(agent, cfg) == 0
    assert len(agent.get_state(cfg).values["messages"]) == 3


def test_repair_is_idempotent():
    agent, cfg = _agent(), {"configurable": {"thread_id": "t3"}}
    agent.update_state(cfg, {"messages": [
        HumanMessage(content="go"), AIMessage(content="", tool_calls=[_call(1)])]}, as_node="agent")
    assert repair_dangling_tool_calls(agent, cfg) == 1
    assert repair_dangling_tool_calls(agent, cfg) == 0


def test_a_misordered_history_is_rebuilt_so_each_result_follows_its_call():
    """The real failure: a new instruction had been recorded between a tool call and its
    (late) result, so the result was in the wrong place and providers rejected it."""
    agent, cfg = _agent(), {"configurable": {"thread_id": "t4"}}
    agent.update_state(cfg, {"messages": [
        HumanMessage(content="first"), AIMessage(content="", tool_calls=[_call(1)]),
        HumanMessage(content="steer"),
        ToolMessage(content="late", tool_call_id="call_1", name="excel_dump_region"),
    ]}, as_node="agent")
    assert repair_dangling_tool_calls(agent, cfg) >= 1
    kinds = [type(m).__name__ for m in agent.get_state(cfg).values["messages"]]
    assert kinds == ["HumanMessage", "AIMessage", "ToolMessage", "HumanMessage"]
    assert repair_dangling_tool_calls(agent, cfg) == 0


def test_every_call_in_the_rebuilt_history_is_answered_immediately():
    agent, cfg = _agent(), {"configurable": {"thread_id": "t5"}}
    agent.update_state(cfg, {"messages": [
        HumanMessage(content="go"),
        AIMessage(content="", tool_calls=[_call(1), _call(2, "read_pdf")]),
        HumanMessage(content="steer"),
        ToolMessage(content="r2", tool_call_id="call_2", name="read_pdf"),
    ]}, as_node="agent")
    repair_dangling_tool_calls(agent, cfg)
    msgs = agent.get_state(cfg).values["messages"]
    ai = next(i for i, m in enumerate(msgs) if isinstance(m, AIMessage))
    after = msgs[ai + 1: ai + 3]
    assert all(isinstance(m, ToolMessage) for m in after)
    assert [m.tool_call_id for m in after] == ["call_1", "call_2"]


def test_a_truncated_tool_call_is_answered_so_the_thread_stays_usable():
    """The model hit its output cap mid-JSON while writing a big report_save_facts call.
    LangChain keeps that as an *invalid* tool call and still sends it to the provider as
    a tool call -- unanswered, which the provider rejects with a 400 forever after."""
    agent, cfg = _agent(), {"configurable": {"thread_id": "t6"}}
    bad = AIMessage(content="", invalid_tool_calls=[{
        "type": "invalid_tool_call", "id": "call_bad", "name": "report_save_facts",
        "args": '{"facts_json": "[{\\"label\\":\\"x\\",\\"value\\":8504', "error": "bad json"}])
    agent.update_state(cfg, {"messages": [HumanMessage(content="go"), bad]}, as_node="agent")
    assert repair_dangling_tool_calls(agent, cfg) == 1
    msgs = agent.get_state(cfg).values["messages"]
    answer = msgs[-1]
    assert isinstance(answer, ToolMessage) and answer.tool_call_id == "call_bad"
    assert "smaller pieces" in answer.content
    assert repair_dangling_tool_calls(agent, cfg) == 0
