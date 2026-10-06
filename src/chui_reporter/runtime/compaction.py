"""Keeping the agent's context useful as a run grows, the way mature agents do it.

What the field does, and what this takes from it:

* Clear old tool results rather than summarise them (JetBrains Research, "The Complexity Trap": observation
  masking matched LLM summarisation at about half the cost, and summaries tended to lengthen runs and hide
  failing trajectories). The agent's own actions and reasoning stay; only the bulky observations shrink.
* Act late, at a share of the model's *real* window (DeepSeek's agent research starts managing context at 80%;
  Cline's auto-compact at about 80%), measured with the provider's own token counts.
* Keep durable state outside the conversation (Anthropic's structured note-taking). Here that is the database:
  the fact ledger, the report, the review notes. Whatever is dropped from the conversation is not lost.
* At the upper threshold, drop old tool history wholesale and continue from a brief rebuilt from that state
  (the "discard" strategies DeepSeek found as effective as summarising, at far fewer steps).
* Push bulky work out of the conversation altogether with subagents (see agents/): a researcher that reads
  fifty web pages returns a page of verified figures, and the pages never enter this context.

One more constraint that is specific to paid APIs: providers cache the *start* of a prompt (DeepSeek bills a
cache hit at about 1/50th of a miss), so rewriting early messages is expensive. Hence every decision here is made
once and frozen: the boundary up to which results are cleared only ever moves forward, in large steps, so the
prefix sent to the model stays byte-identical from call to call and keeps hitting the cache.

The saved thread is never rewritten; this builds the *view* sent to the model.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

KEEP_RECENT = 14                 # messages at the end that are never touched
STUB_CHARS = 260                 # how much of a cleared tool result is kept
DEFAULT_MASK_FRACTION = 0.5      # start clearing old tool results at this share of the usable window
DEFAULT_DISCARD_FRACTION = 0.8   # drop old tool history at this share (DeepSeek's own threshold)


def _chars(m: BaseMessage) -> int:
    c = m.content
    n = len(c) if isinstance(c, str) else sum(len(str(b)) for b in c)
    for tc in getattr(m, "tool_calls", None) or []:
        n += len(str(tc.get("args", ""))) + len(tc.get("name", ""))
    return n


def estimate_tokens(messages: list[BaseMessage]) -> int:
    """A pessimistic estimate (3.5 characters a token). Only used between provider measurements."""
    return int(sum(_chars(m) for m in messages) / 3.5)


def _stub(m: ToolMessage) -> ToolMessage:
    body = str(m.content)
    if len(body) <= STUB_CHARS * 2:
        return m
    head = body[:STUB_CHARS].rstrip()
    return ToolMessage(content=f"{head}\n[... older result cleared to save context; call the tool again if you need it]",
                       tool_call_id=m.tool_call_id, name=m.name, status=getattr(m, "status", "success"), id=m.id)


def _safe_cut(messages: list[BaseMessage], at: int) -> int:
    """A cut point at or before `at` that never falls between a call and its result."""
    cut = max(1, min(at, len(messages)))
    while cut > 1 and cut < len(messages) and isinstance(messages[cut], ToolMessage):
        cut -= 1
    return cut


class ContextMeter:
    """How full the model's context really is.

    The character estimate knows nothing of the system prompt or the tool definitions, so it is calibrated
    against the provider's own count: every reply carries `usage_metadata.input_tokens`, the exact size of
    the prompt that produced it. The estimate is scaled to match, and decisions use the calibrated figure."""

    def __init__(self, window: int, *, reserve: int = 32_000, overhead: int = 0, fraction: float | None = None,
                 discard_fraction: float | None = None, budget: int | None = None):
        self.window, self.overhead = window, overhead
        usable = max(8_000, window - reserve)
        frac = fraction if fraction is not None else float(os.environ.get("CHUI_CONTEXT_FRACTION", DEFAULT_MASK_FRACTION))
        dfrac = discard_fraction if discard_fraction is not None else float(
            os.environ.get("CHUI_CONTEXT_DISCARD_FRACTION", DEFAULT_DISCARD_FRACTION))
        override = budget if budget is not None else (int(os.environ["CHUI_CONTEXT_BUDGET"]) if os.environ.get("CHUI_CONTEXT_BUDGET") else None)
        self.mask_at = override or max(8_000, int(usable * frac))
        self.discard_at = int(override * 1.6) if override else max(self.mask_at + 4_000, int(usable * dfrac))
        self.factor = 1.0
        self.last_real: int | None = None       # tokens in the prompt of the latest reply, as the provider counted them
        self._seen = -1
        self._sent_est: int | None = None

    def scale_thresholds(self, factor: float) -> None:
        self.mask_at = max(8_000, int(self.mask_at * factor))
        self.discard_at = max(self.mask_at + 4_000, int(self.discard_at * factor))

    def observe(self, messages: list[BaseMessage]) -> None:
        for i in range(len(messages) - 1, -1, -1):
            m = messages[i]
            u = getattr(m, "usage_metadata", None) if isinstance(m, AIMessage) else None
            if u and u.get("input_tokens"):
                if i != self._seen:
                    self._seen = i
                    self.last_real = int(u["input_tokens"])
                    if self._sent_est:
                        self.factor = min(4.0, max(0.5, (self.last_real - self.overhead) / self._sent_est))
                return

    def sent(self, view: list[BaseMessage]) -> None:
        self._sent_est = max(1, estimate_tokens(view))

    def used(self, messages: list[BaseMessage]) -> int:
        return int(estimate_tokens(messages) * self.factor) + self.overhead

    def snapshot(self, messages: list[BaseMessage] | None = None) -> dict:
        used = self.last_real if self.last_real is not None else (self.used(messages) if messages else 0)
        return {"used_tokens": used, "window": self.window, "mask_at": self.mask_at, "discard_at": self.discard_at,
                "measured": self.last_real is not None}


class Compactor:
    """Builds the view of the conversation sent to the model, deciding as late as possible and then not
    changing its mind (see the module note on prompt caching)."""

    def __init__(self, meter: ContextMeter, brief: Callable[[], str] | None = None):
        self.meter, self.brief = meter, brief
        self.mask_upto = 0                       # tool results before this index are cleared
        self.cut = 0                             # messages between the first and this index are dropped
        self._note: HumanMessage | None = None   # the text that replaces them, fixed when the cut was made

    def _apply(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        view = [(_stub(m) if isinstance(m, ToolMessage) and i < self.mask_upto else m) for i, m in enumerate(messages)]
        if self.cut:
            cut = _safe_cut(view, self.cut)
            view = view[:1] + ([self._note] if self._note else []) + view[cut:]
        return view

    def view(self, messages: list[BaseMessage]) -> tuple[list[BaseMessage], dict]:
        meter = self.meter
        meter.observe(messages)
        view = self._apply(messages)
        stats: dict = {}
        before = meter.used(view)
        if before > meter.mask_at and len(messages) > KEEP_RECENT + 1:
            target = _safe_cut(messages, len(messages) - KEEP_RECENT)
            if target > self.mask_upto:                                  # advance in one large step, never back
                by_tool: dict[str, int] = {}
                for i in range(self.mask_upto, target):
                    m = messages[i]
                    if isinstance(m, ToolMessage) and _stub(m) is not m:
                        by_tool[m.name or "tool"] = by_tool.get(m.name or "tool", 0) + 1
                self.mask_upto = target
                view = self._apply(messages)
                stats = {"stage": 1, "shortened": sum(by_tool.values()), "by_tool": by_tool}
        after = meter.used(view)
        if after > meter.discard_at and len(messages) > KEEP_RECENT + 2:
            target = _safe_cut(messages, len(messages) - KEEP_RECENT)
            if target > self.cut:
                text = ""
                if self.brief:
                    try:
                        text = self.brief()
                    except Exception:                                    # noqa: BLE001 - a brief is a nicety, never a failure
                        text = ""
                self._note = HumanMessage(content=(
                    "[Earlier steps in this run were set aside to keep the conversation within the model's limits. "
                    "Nothing recorded was lost: every figure you read is in the fact ledger (ledger_search), every "
                    "section you wrote is in the report (report_outline), and every data problem is a review note.]"
                    + (f"\n{text}" if text else "")))
                dropped = target - max(1, self.cut)
                self.cut = target
                view = self._apply(messages)
                stats = {**stats, "stage": 2, "dropped": dropped}
                after = meter.used(view)
        if stats:
            stats.update(meter.snapshot(messages))
            stats.update({"before": before, "after": after, "messages_before": len(messages), "messages_after": len(view)})
        meter.sent(view)
        return view, stats


def make_hook(meter: ContextMeter, brief: Callable[[], str] | None = None,
              on_compact: Callable[[dict], None] | None = None) -> Callable:
    """A `pre_model_hook` for create_react_agent. Returns a view, not a state edit."""
    compactor = Compactor(meter, brief)

    def hook(state):
        view, stats = compactor.view(list(state["messages"]))
        if stats and on_compact:
            try:
                on_compact(stats)
            except Exception:                    # noqa: BLE001
                pass
        return {"llm_input_messages": view}

    hook.compactor = compactor
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
