"""Provider-agnostic chat model factory.

The agent is written against LangChain's `BaseChatModel`, so the provider is a
configuration choice, not a code change:

    CHUI_PROVIDER=deepseek     (default)
    CHUI_PROVIDER=anthropic

Each provider has its own key and its own model variable, so flipping the
provider never leaves a model name from the other vendor behind.

DeepSeek notes, verified against its API docs (api-docs.deepseek.com):

* Models are `deepseek-flash` (V4.1-Flash) and `deepseek-v4-pro`. The old
  `deepseek-chat` / `deepseek-reasoner` names are retired. Both support tool
  calls.
* Thinking mode is ON by default at the API. With tools in the request, the
  `reasoning_content` of every previous turn must be sent back. LangChain's
  DeepSeek adapter captures it on responses but does not return it on the next
  request, which breaks the second step of any ReAct loop. So:

    - default is thinking OFF -- the documented tool path, and it permits
      temperature=0, which thinking mode ignores;
    - CHUI_THINKING=on enables it through `ChatDeepSeekThinking`, which
      round-trips the reasoning explicitly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage

DEFAULT_PROVIDER = "deepseek"


class ProviderError(RuntimeError):
    """Misconfiguration the user can fix: unknown provider, missing key."""


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    key_env: str
    model_env: str
    default_model: str


PROVIDERS: dict[str, ProviderSpec] = {
    "deepseek": ProviderSpec("deepseek", "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "deepseek-flash"),
    "anthropic": ProviderSpec(
        "anthropic", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "claude-sonnet-5-5"
    ),
}


def resolve_provider(name: str | None = None) -> ProviderSpec:
    chosen = (name or os.environ.get("CHUI_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    if chosen not in PROVIDERS:
        raise ProviderError(
            f"unknown provider {chosen!r}; choose one of {sorted(PROVIDERS)} "
            f"(set CHUI_PROVIDER or pass --provider)"
        )
    return PROVIDERS[chosen]


def resolve_model(spec: ProviderSpec, model: str | None = None) -> str:
    return (model or os.environ.get(spec.model_env) or spec.default_model).strip()


def api_key(spec: ProviderSpec) -> str | None:
    return os.environ.get(spec.key_env, "").strip() or None


def require_key(spec: ProviderSpec) -> str:
    key = api_key(spec)
    if not key:
        raise ProviderError(
            f"{spec.key_env} is not set (provider {spec.name!r}). "
            f"Put it in chui-reporter/.env, or switch with CHUI_PROVIDER."
        )
    return key


def thinking_enabled(override: bool | None = None) -> bool:
    if override is not None:
        return override
    return os.environ.get("CHUI_THINKING", "").strip().lower() in {"1", "true", "on", "yes"}


def _chat_deepseek_thinking():
    """Defined lazily so importing this module does not import langchain-deepseek."""
    from langchain_deepseek import ChatDeepSeek

    class ChatDeepSeekThinking(ChatDeepSeek):
        """ChatDeepSeek that returns `reasoning_content` on tool-loop requests.

        DeepSeek requires the reasoning of all previous turns whenever the
        request carries tools. Outgoing assistant messages line up one-to-one
        with the AIMessages in the history, so it is re-attached by position.
        A count mismatch raises rather than guessing: a wrong attachment would
        silently corrupt the model's context.
        """

        def _get_request_payload(self, input_, *, stop=None, **kwargs):
            payload = super()._get_request_payload(input_, stop=stop, **kwargs)
            sent = [m for m in payload["messages"] if m.get("role") == "assistant"]
            history = [
                m for m in self._convert_input(input_).to_messages() if isinstance(m, AIMessage)
            ]
            if len(sent) != len(history):
                raise RuntimeError(
                    f"cannot align reasoning_content: {len(history)} assistant turns in "
                    f"history but {len(sent)} in the request payload"
                )
            for out, src in zip(sent, history):
                reasoning = src.additional_kwargs.get("reasoning_content")
                if reasoning:
                    out["reasoning_content"] = reasoning
            return payload

    return ChatDeepSeekThinking


def get_llm(
    provider: str | None = None,
    model: str | None = None,
    *,
    thinking: bool | None = None,
    effort: str | None = None,
) -> BaseChatModel:
    spec = resolve_provider(provider)
    key = require_key(spec)
    name = resolve_model(spec, model)

    if spec.name == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=name, api_key=key, max_tokens=16000, temperature=0)

    # deepseek
    think = thinking_enabled(thinking)
    if think:
        cls = _chat_deepseek_thinking()
        return cls(
            model=name,
            api_key=key,
            max_tokens=32000,
            reasoning_effort=effort or os.environ.get("CHUI_REASONING_EFFORT", "high"),
            extra_body={"thinking": {"type": "enabled"}},
        )
    from langchain_deepseek import ChatDeepSeek

    return ChatDeepSeek(
        model=name,
        api_key=key,
        max_tokens=32000,
        temperature=0,
        extra_body={"thinking": {"type": "disabled"}},
    )


def describe(provider: str | None = None, model: str | None = None,
             thinking: bool | None = None) -> str:
    spec = resolve_provider(provider)
    extra = ""
    if spec.name == "deepseek":
        extra = ", thinking on" if thinking_enabled(thinking) else ", thinking off"
    return f"{spec.name}/{resolve_model(spec, model)}{extra}"
