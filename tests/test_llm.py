"""Provider switching and request payloads, all offline.

The payload tests matter most: they pin what is actually sent to DeepSeek,
because its thinking-mode contract (reasoning must be returned on tool-bearing
requests) is the kind of thing that fails only at the second step of a live run.
"""

from __future__ import annotations

import json

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chui_reporter.agent import llm as L


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in ("CHUI_PROVIDER", "CHUI_THINKING", "CHUI_REASONING_EFFORT", "DEEPSEEK_MODEL",
              "ANTHROPIC_MODEL", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(v, raising=False)


def _wire(llm, messages="hi"):
    """The JSON body actually sent over HTTP, captured with a mock transport.

    `extra_body` (where the thinking toggle lives) is merged by the OpenAI
    client at the HTTP layer and never appears in `_get_request_payload`, so
    only the wire body proves what DeepSeek receives."""
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ok"}}]})

    llm.client = openai.OpenAI(
        api_key="x", base_url="https://api.deepseek.com",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).chat.completions
    llm.invoke(messages)
    return captured["body"]


def _history(reasoning="I should list sources first."):
    ai = AIMessage(
        content="",
        tool_calls=[{"name": "list_sources", "args": {}, "id": "call_1", "type": "tool_call"}],
        additional_kwargs={"reasoning_content": reasoning} if reasoning else {},
    )
    return [HumanMessage(content="go"), ai, ToolMessage(content="files", tool_call_id="call_1")]


# -- selection -------------------------------------------------------------


def test_deepseek_is_the_default_provider():
    assert L.resolve_provider().name == "deepseek"
    assert L.resolve_model(L.resolve_provider()) == "deepseek-flash"


def test_provider_switch_is_one_variable(monkeypatch):
    monkeypatch.setenv("CHUI_PROVIDER", "anthropic")
    spec = L.resolve_provider()
    assert spec.name == "anthropic"
    assert L.resolve_model(spec) == "claude-sonnet-5-5"


def test_model_names_never_leak_across_providers(monkeypatch):
    """A DeepSeek model override must not be applied when the provider flips."""
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("CHUI_PROVIDER", "anthropic")
    assert L.resolve_model(L.resolve_provider()) == "claude-sonnet-5-5"


def test_cli_argument_beats_environment(monkeypatch):
    monkeypatch.setenv("CHUI_PROVIDER", "anthropic")
    assert L.resolve_provider("deepseek").name == "deepseek"


def test_unknown_provider_is_a_clear_error():
    with pytest.raises(L.ProviderError, match="unknown provider"):
        L.resolve_provider("openai")


def test_missing_key_names_the_variable_for_that_provider():
    with pytest.raises(L.ProviderError, match="DEEPSEEK_API_KEY"):
        L.get_llm("deepseek")
    with pytest.raises(L.ProviderError, match="ANTHROPIC_API_KEY"):
        L.get_llm("anthropic")


def test_blank_key_counts_as_missing(monkeypatch):
    """.env ships with empty values; an empty string must not pass for a key."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "   ")
    with pytest.raises(L.ProviderError):
        L.get_llm("deepseek")


# -- DeepSeek request payloads ----------------------------------------------


def test_deepseek_default_disables_thinking_and_keeps_temperature(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-offline")
    body = _wire(L.get_llm("deepseek"))
    assert body["model"] == "deepseek-flash"
    assert body["thinking"] == {"type": "disabled"}
    assert body["temperature"] == 0


def test_deepseek_thinking_mode_requests_thinking_and_effort(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-offline")
    body = _wire(L.get_llm("deepseek", thinking=True, effort="max"))
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "max"
    assert "temperature" not in body, "thinking mode ignores temperature; do not send it"


def test_thinking_can_be_enabled_by_environment(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-offline")
    monkeypatch.setenv("CHUI_THINKING", "on")
    assert L.thinking_enabled() is True
    assert _wire(L.get_llm("deepseek"))["thinking"] == {"type": "enabled"}


def test_reasoning_survives_to_the_wire_on_a_tool_turn(monkeypatch):
    """End to end: history -> adapter -> HTTP body."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-offline")
    body = _wire(L.get_llm("deepseek", thinking=True), _history("wire thought"))
    assistant = [m for m in body["messages"] if m["role"] == "assistant"]
    assert assistant[0]["reasoning_content"] == "wire thought"


def test_stock_adapter_drops_reasoning_content_which_is_why_we_subclass(monkeypatch):
    """Pins the defect that motivated ChatDeepSeekThinking. If this starts
    failing, langchain-deepseek has fixed it upstream and the subclass can go."""
    from langchain_deepseek import ChatDeepSeek

    llm = ChatDeepSeek(model="deepseek-flash", api_key="sk-offline")
    sent = [m for m in llm._get_request_payload(_history())["messages"] if m["role"] == "assistant"]
    assert "reasoning_content" not in sent[0]


def test_thinking_mode_returns_reasoning_on_tool_turns(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-offline")
    llm = L.get_llm("deepseek", thinking=True)
    sent = [m for m in llm._get_request_payload(_history())["messages"] if m["role"] == "assistant"]
    assert sent[0]["reasoning_content"] == "I should list sources first."


def test_reasoning_is_attached_to_the_right_turn(monkeypatch):
    """Two assistant turns, different reasoning: each must land on its own turn."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-offline")
    llm = L.get_llm("deepseek", thinking=True)
    first = _history("first thought")
    second_ai = AIMessage(
        content="",
        tool_calls=[{"name": "report_outline", "args": {}, "id": "call_2", "type": "tool_call"}],
        additional_kwargs={"reasoning_content": "second thought"},
    )
    msgs = first + [second_ai, ToolMessage(content="outline", tool_call_id="call_2")]
    sent = [m for m in llm._get_request_payload(msgs)["messages"] if m["role"] == "assistant"]
    assert [m["reasoning_content"] for m in sent] == ["first thought", "second thought"]


def test_turn_without_reasoning_is_left_alone(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-offline")
    llm = L.get_llm("deepseek", thinking=True)
    sent = [m for m in llm._get_request_payload(_history(reasoning=None))["messages"]
            if m["role"] == "assistant"]
    assert "reasoning_content" not in sent[0]


# -- Anthropic --------------------------------------------------------------


def test_anthropic_provider_builds(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-offline")
    llm = L.get_llm("anthropic")
    assert type(llm).__name__ == "ChatAnthropic"
    assert llm.model == "claude-sonnet-5-5"


def test_describe_states_provider_model_and_mode(monkeypatch):
    assert L.describe("deepseek") == "deepseek/deepseek-flash, thinking off"
    assert L.describe("deepseek", thinking=True) == "deepseek/deepseek-flash, thinking on"
    assert L.describe("anthropic") == "anthropic/claude-sonnet-5-5"


# -- the agent graph is provider-independent ---------------------------------


@pytest.mark.parametrize("provider,key", [("deepseek", "DEEPSEEK_API_KEY"),
                                          ("anthropic", "ANTHROPIC_API_KEY")])
def test_agent_builds_with_either_provider_and_all_tools(monkeypatch, provider, key):
    from chui_reporter.agent.graph import build_agent
    from chui_reporter.agent.tools import ALL_TOOLS

    monkeypatch.setenv(key, "sk-offline")
    agent = build_agent(None, provider)
    assert len(agent.nodes["tools"].bound.tools_by_name) == len(ALL_TOOLS)
