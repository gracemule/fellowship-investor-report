"""Keeping the agent's context small without losing anything that matters.

The agent's memory is the database, not the transcript. Every figure it has read is in the
fact ledger with its source, every section it has written is in the report tables, every
data problem is a review note. So the transcript can be cut aggressively -- as long as what
is cut is old and what remains is a valid conversation.

This runs before every model call (`pre_model_hook`) and returns a trimmed *view*; the saved
thread history is never rewritten, so nothing is lost for audit and a resumed run can be
trimmed again identically.

Stages, applied only while over budget:
  1. old tool results shrink to their opening lines (the agent already acted on them);
  2. old exchanges are dropped whole, and replaced by a short brief rebuilt from the database
     -- never mid-exchange, because a tool call without its result is a provider error.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

DEFAULT_BUDGET = int(os.environ.get("CHUI_CONTEXT_BUDGET", "80000"))   # tokens
KEEP_RECENT = 14                                                        # messages never touched
STUB_CHARS = 260


def _chars(m: BaseMessage) -> int:
    c = m.content
    n = len(c) if isinstance(c, str) else sum(len(str(b)) for b in c)
    for tc in getattr(m, "tool_calls", None) or []:
        n += len(str(tc.get("args", ""))) + len(tc.get("name", ""))
    return n


def estimate_tokens(messages: list[BaseMessage]) -> int:
    """A deliberately pessimistic estimate (3.5 characters a token): numbers and JSON tokenise
    worse than prose, and being over-cautious here only costs a little trimming."""
    return int(sum(_chars(m) for m in messages) / 3.5)


def _stub(m: ToolMessage) -> ToolMessage:
    body = str(m.content)
    if len(body) <= STUB_CHARS * 2:
        return m
    head = body[:STUB_CHARS].rstrip()
    return ToolMessage(content=f"{head}\n[... older result shortened; its figures are in the fact ledger]",
                       tool_call_id=m.tool_call_id, name=m.name, status=getattr(m, "status", "success"),
                       id=m.id)


def _safe_cut(messages: list[BaseMessage], want_tail: int) -> int:
    """Index where the kept tail starts: never on a ToolMessage (its call would be cut off)."""
    cut = max(1, len(messages) - want_tail)
    while cut > 1 and isinstance(messages[cut], ToolMessage):
        cut -= 1
    return cut


def compact(messages: list[BaseMessage], budget: int = DEFAULT_BUDGET, *,
            brief: Callable[[], str] | None = None) -> tuple[list[BaseMessage], dict]:
    """Returns (messages to send, stats). Stats is {} when nothing was trimmed."""
    before = estimate_tokens(messages)
    if before <= budget or len(messages) <= KEEP_RECENT:
        return messages, {}

    protect_from = max(0, len(messages) - KEEP_RECENT)
    view = [(_stub(m) if isinstance(m, ToolMessage) and i < protect_from else m)
            for i, m in enumerate(messages)]
    by_tool: dict[str, int] = {}
    for a, b in zip(messages, view):
        if a is not b:
            by_tool[a.name or "tool"] = by_tool.get(a.name or "tool", 0) + 1
    shortened = sum(by_tool.values())
    stage = 1
    if estimate_tokens(view) > budget:
        stage = 2
        cut = _safe_cut(view, KEEP_RECENT)
        head = view[:1]                          # the original instruction
        text = ""
        if brief:
            try:
                text = brief()
            except Exception:                    # noqa: BLE001 - a brief is a nicety, never a failure
                text = ""
        note = HumanMessage(content=(
            "[Earlier steps in this run were condensed to keep the conversation short. "
            "Nothing was lost: every figure you read is in the fact ledger (ledger_search), every "
            "section you wrote is in the report (report_outline).]" + (f"\n{text}" if text else "")))
        view = head + [note] + view[cut:]
    return view, {"stage": stage, "before": before, "after": estimate_tokens(view),
                  "messages_before": len(messages), "messages_after": len(view),
                  "shortened": shortened, "by_tool": by_tool, "dropped": len(messages) - len(view) + (1 if stage == 2 else 0)}


def make_hook(budget: int = DEFAULT_BUDGET, brief: Callable[[], str] | None = None,
              on_compact: Callable[[dict], None] | None = None):
    """A `pre_model_hook` for create_react_agent. Returns the trimmed view, not a state edit."""
    def hook(state):
        view, stats = compact(list(state["messages"]), budget, brief=brief)
        if stats and on_compact:
            try:
                on_compact(stats)
            except Exception:                    # noqa: BLE001
                pass
        return {"llm_input_messages": view}
    return hook


def store_brief(store) -> str:
    """Where the work stands, read from the database."""
    c = store.completion()
    counts = store.status_counts()
    secs = store.sections() if hasattr(store, "sections") else []
    keys = ", ".join(str(s["key"]) for s in secs if s.get("present", True)) if secs else "none yet"
    notes = len(store.review_notes())
    facts = ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "none yet"
    return (f"Where the work stands: facts recorded: {facts}. Sections written: {keys}. "
            f"Review notes: {notes}. Rendered since last change: {'yes' if c['rendered'] else 'no'}. "
            f"Pages inspected since last render: {'yes' if c['inspected'] else 'no'}.")
