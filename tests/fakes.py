"""A scripted chat model: replays a fixed list of replies, so graph behaviour (pause, resume,
retry, compaction) is tested without a provider and without spending anything."""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult


def call(name: str, args: dict, id_: str, usage: dict | None = None) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": id_, "type": "tool_call"}],
                     usage_metadata=usage)


def calls(*specs) -> AIMessage:
    """One reply that asks for several tools at once, as models often do: calls(("a", {}, "id1"), ("b", {}, "id2"))."""
    return AIMessage(content="", tool_calls=[{"name": n, "args": a, "id": i, "type": "tool_call"} for n, a, i in specs])


class ScriptedChat(BaseChatModel):
    script: list[Any]                    # AIMessage, or an Exception instance to raise
    seen: list[list[BaseMessage]] = []
    i: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.seen.append(list(messages))
        if self.i >= len(self.script):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="done"))])
        step = self.script[self.i]
        self.i += 1
        if isinstance(step, Exception):
            raise step
        return ChatResult(generations=[ChatGeneration(message=step)])


class RoutedChat(BaseChatModel):
    """One model standing in for several subagents at once: the script used is chosen by which key appears in the
    first human message (the task), each with its own position, so parallel subagents cannot disturb one another."""

    routes: dict[str, list[Any]]
    pos: dict[str, int] = {}
    seen: list[list[BaseMessage]] = []
    lock: Any = None

    @property
    def _llm_type(self) -> str:
        return "routed"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        import threading

        first = next((str(m.content) for m in messages if m.type == "human"), "")
        key = next((k for k in self.routes if k in first), None)
        if key is None:
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="{}"))])
        if self.lock is None:
            object.__setattr__(self, "lock", threading.Lock())
        with self.lock:
            self.seen.append(list(messages))
            i = self.pos.get(key, 0)
            self.pos[key] = i + 1
        script = self.routes[key]
        step = script[i] if i < len(script) else AIMessage(content="{}")
        if isinstance(step, Exception):
            raise step
        return ChatResult(generations=[ChatGeneration(message=step)])
