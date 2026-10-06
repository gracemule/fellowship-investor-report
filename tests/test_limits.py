"""The model's real limits come from the provider, and context decisions use the provider's own token counts."""

from __future__ import annotations

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chui_reporter.agent import limits as L
from chui_reporter.runtime.compaction import ContextMeter, make_hook


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    L._CACHE.clear()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("CHUI_CONTEXT_BUDGET", raising=False)
    monkeypatch.delenv("CHUI_CONTEXT_FRACTION", raising=False)


DEEPSEEK = {"data": [{"id": "deepseek-flash", "context_window": 1048576, "max_output_tokens": 393216},
                     {"id": "deepseek-v4-pro", "context_window": 1048576, "max_output_tokens": 393216}]}


def test_deepseek_limits_are_read_from_its_models_endpoint():
    seen = []

    def handler(req):
        seen.append((str(req.url), req.headers["authorization"]))
        return httpx.Response(200, json=DEEPSEEK)

    lim = L.get_limits("deepseek", "deepseek-flash", client=_client(handler))
    assert (lim.context_window, lim.max_output, lim.source) == (1048576, 393216, "provider")
    assert seen == [("https://api.deepseek.com/models", "Bearer k")]
    L.get_limits("deepseek", "deepseek-flash", client=_client(handler))
    assert len(seen) == 1, "asked once, then remembered"


def test_anthropic_limits_use_its_model_endpoint():
    def handler(req):
        assert req.url.path == "/v1/models/claude-sonnet-5-5" and req.headers["x-api-key"] == "k"
        return httpx.Response(200, json={"id": "claude-sonnet-5-5", "max_input_tokens": 200000, "max_tokens": 64000})

    lim = L.get_limits("anthropic", "claude-sonnet-5-5", client=_client(handler))
    assert (lim.context_window, lim.max_output) == (200000, 64000)


@pytest.mark.parametrize("handler", [
    lambda r: httpx.Response(401),
    lambda r: httpx.Response(200, json={"data": [{"id": "something-else", "context_window": 5}]}),
    lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")),
])
def test_when_the_provider_cannot_be_asked_a_small_assumption_is_used_and_said_so(handler):
    lim = L.get_limits("deepseek", "deepseek-flash", client=_client(handler))
    assert lim.context_window == L.FALLBACK_WINDOW and lim.source.startswith("assumed")


def _reply(input_tokens: int, output_tokens: int = 50, calls=True):
    return AIMessage(content="ok", usage_metadata={"input_tokens": input_tokens, "output_tokens": output_tokens,
                                                  "total_tokens": input_tokens + output_tokens})


def test_the_thresholds_are_shares_of_the_real_window_not_a_guess():
    m = ContextMeter(1_048_576, reserve=32_000)
    usable = 1_048_576 - 32_000
    assert m.mask_at == int(usable * 0.5) and m.discard_at == int(usable * 0.8)   # DeepSeek's own 80% for the upper one
    assert ContextMeter(1_048_576, fraction=0.25).mask_at < m.mask_at
    o = ContextMeter(1_048_576, budget=80_000)
    assert o.mask_at == 80_000 and o.discard_at > o.mask_at, "an explicit override still wins"


def test_the_estimate_is_calibrated_against_the_providers_own_count():
    m = ContextMeter(1_000_000, overhead=8_000)
    view = [HumanMessage(content="x" * 35_000)]                             # ~10,000 estimated tokens
    m.sent(view)
    m.observe(view + [_reply(input_tokens=28_000)])                         # the provider says the prompt was 28,000
    assert m.last_real == 28_000
    assert 1.9 < m.factor < 2.1, "the estimator was about half the real size once overhead is allowed for"
    assert m.snapshot()["measured"] is True and m.snapshot()["used_tokens"] == 28_000


def test_a_small_conversation_is_never_trimmed_on_a_large_window():
    """The macro session's trimming at ~80k tokens of a 1M window was pure loss: it broke the prompt cache."""
    msgs = [HumanMessage(content="go")]
    for i in range(80):
        msgs += [AIMessage(content="", tool_calls=[{"name": "web_fetch", "args": {"url": str(i)}, "id": f"c{i}", "type": "tool_call"}]),
                 ToolMessage(content="x" * 4000, tool_call_id=f"c{i}", name="web_fetch")]
    hook = make_hook(ContextMeter(1_048_576, overhead=10_000))
    out = hook({"messages": msgs})["llm_input_messages"]
    assert out == msgs, "nothing is rewritten while the conversation is far below the limit"


def test_trimming_starts_only_when_the_conversation_nears_the_threshold():
    msgs = [HumanMessage(content="go")]
    for i in range(80):
        msgs += [AIMessage(content="", tool_calls=[{"name": "web_fetch", "args": {"url": str(i)}, "id": f"c{i}", "type": "tool_call"}]),
                 ToolMessage(content="x" * 4000, tool_call_id=f"c{i}", name="web_fetch")]
    events = []
    hook = make_hook(ContextMeter(60_000, reserve=10_000, overhead=1_000), on_compact=events.append)
    out = hook({"messages": msgs})["llm_input_messages"]
    assert out != msgs and events and events[0]["stage"] in (1, 2)
    assert events[0]["window"] == 60_000 and events[0]["used_tokens"] > 0
