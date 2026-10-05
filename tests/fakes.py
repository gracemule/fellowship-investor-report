"""A scripted chat model: replays a fixed list of replies, so graph behaviour (pause, resume,
retry, compaction) is tested without a provider and without spending anything."""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult


def call(name: str, args: dict, id_: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": id_, "type": "tool_call"}])


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
