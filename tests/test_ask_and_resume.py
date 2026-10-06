"""The agent can stop, ask a person, and carry on from exactly where it was -- including
after its process died. This is what lets it run alone and still ask for help."""

from __future__ import annotations

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from chui_reporter.agent import tools as T
from chui_reporter.agent.graph import build_agent
from tests.fakes import ScriptedChat, call


def _agent(script, saver):
    T.reset_questions()
    llm = ScriptedChat(script=script, seen=[])
    return build_agent(saver, llm=llm, tools=[T.ask_user, T.request_sources], breakpoints=False), llm


def test_ask_user_pauses_then_resumes_with_the_answer():
    saver = InMemorySaver()
    agent, llm = _agent([
        call("ask_user", {"question": "Which fair value should the report use?",
                          "why_it_matters": "It sets 2.1 and 5.4.",
                          "options": ["Fund Model", "Valuation reports"]}, "q1"),
    ], saver)
    cfg = {"configurable": {"thread_id": "t"}}
    out = agent.invoke({"messages": [HumanMessage("go")]}, cfg)
    state = agent.get_state(cfg)
    assert state.next == ("tools",)                                   # parked on the tool
    payload = state.tasks[0].interrupts[0].value
    assert payload["kind"] == "info" and payload["options"] == ["Fund Model", "Valuation reports"]

    agent.invoke(Command(resume="Fund Model"), cfg)
    last_tool = [m for m in agent.get_state(cfg).values["messages"] if m.type == "tool"][-1]
    assert "Fund Model" in last_tool.content


def test_the_pause_survives_a_new_process():
    """A different graph object, same checkpointer: what a restart looks like."""
    saver = InMemorySaver()
    a1, _ = _agent([call("ask_user", {"question": "Q?", "why_it_matters": "W."}, "q1")], saver)
    cfg = {"configurable": {"thread_id": "t"}}
    a1.invoke({"messages": [HumanMessage("go")]}, cfg)
    a2, _ = _agent([], saver)
    assert a2.get_state(cfg).tasks[0].interrupts[0].value["prompt"] == "Q?"
    a2.invoke(Command(resume="yes"), cfg)
    assert any("yes" in str(m.content) for m in a2.get_state(cfg).values["messages"] if m.type == "tool")


def test_questions_are_budgeted_and_a_replay_is_not_double_counted():
    saver = InMemorySaver()
    calls = [call("ask_user", {"question": f"Q{i}?", "why_it_matters": "w"}, f"c{i}") for i in range(5)]
    agent, _ = _agent(calls, saver)
    cfg = {"configurable": {"thread_id": "t"}, "recursion_limit": 60}
    agent.invoke({"messages": [HumanMessage("go")]}, cfg)
    for _ in range(T.QUESTION_BUDGET):
        agent.invoke(Command(resume="ok"), cfg)
    msgs = agent.get_state(cfg).values["messages"]
    tool_msgs = [m.content for m in msgs if m.type == "tool"]
    assert sum("The user answered" in c for c in tool_msgs) == T.QUESTION_BUDGET
    assert any("used your questions" in c for c in tool_msgs)


def test_request_sources_does_not_pause_when_the_sources_are_there(tmp_path):
    from chui_reporter import config
    old = config.SOURCE_ROOT
    try:
        (tmp_path / "Fund Financials").mkdir()
        (tmp_path / "Fund Financials" / "WP - Chui ventures LP (3).xlsx").write_bytes(b"x")
        config.set_root(tmp_path)
        out = T.request_sources.invoke({"name": "request_sources", "type": "tool_call", "id": "x",
                                        "args": {"slots": ["lp_workpaper"], "why_it_matters": "needed"}})
        assert "already available" in str(out.content if hasattr(out, "content") else out)
    finally:
        config.set_root(old)


def test_repair_never_touches_a_question_that_is_waiting_for_its_answer():
    from chui_reporter.agent.run import repair_dangling_tool_calls
    saver = InMemorySaver()
    agent, _ = _agent([call("ask_user", {"question": "Q?", "why_it_matters": "W."}, "q1")], saver)
    cfg = {"configurable": {"thread_id": "t"}}
    agent.invoke({"messages": [HumanMessage("go")]}, cfg)
    assert repair_dangling_tool_calls(agent, cfg) == 0
    assert agent.get_state(cfg).tasks[0].interrupts                      # still parked
