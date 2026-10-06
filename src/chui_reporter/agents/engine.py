"""Running a subagent: its own conversation, its own budget, structured results, and a record of what it did."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import ToolNode, create_react_agent
from pydantic import BaseModel, Field, ValidationError

from ..agent.store import Store
from ..runtime import narrator
from ..runtime.compaction import ContextMeter, make_hook
from ..runtime.failures import backoff, classify
from . import records

OUT_OF_STEPS = "Sorry, need more steps"


# ---- what a subagent hands back -------------------------------------------------------------------


class Claim(BaseModel):
    key: str = Field(description="short identifier for what this figure is, e.g. 'inflation'")
    label: str = Field(description="plain-words name for the ledger, e.g. 'Kenya inflation'")
    value: float
    unit: str = Field(description="'percent', or e.g. 'KES per USD'")
    as_of: str = Field(max_length=20, description="the period the figure describes, only e.g. 'Jun 2026' or 'Q1 2026'")
    basis: str = Field(default="", max_length=120, description="anything that qualifies it, e.g. 'monthly average', 'year on year'")
    source_url: str
    quote: str = Field(description="the exact sentence or table row on that page that contains the figure")


class Gap(BaseModel):
    key: str
    reason: str


class ResearchResult(BaseModel):
    summary: str = ""
    claims: list[Claim] = []
    gaps: list[Gap] = []


def extract_json(text: str) -> dict:
    """The JSON object in a reply: a fenced block if there is one, else the outermost braces."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    raw = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    if not raw.strip():
        raise ValueError("no JSON object found in the reply")
    return json.loads(raw)


def parse_result(text: str) -> ResearchResult:
    try:
        return ResearchResult.model_validate(extract_json(text))
    except (ValueError, ValidationError) as exc:
        raise ValueError(str(exc)[:400]) from exc


# ---- the definition of a kind of subagent ---------------------------------------------------------


@dataclass(frozen=True)
class Subagent:
    name: str
    title: str
    prompt: str
    tools: tuple
    max_rounds: int = 22                           # tool rounds before it is told to wrap up
    budgets: dict = field(default_factory=dict)    # tool name -> maximum calls


@dataclass
class Context:
    """What a subagent needs from the process around it. The runner fills this in; tests fill in stand-ins."""
    store: Store
    report_id: str
    run_id: str | None = None
    session_id: str | None = None
    emit: Callable[..., None] = lambda kind, label="", **kw: None
    should_stop: Callable[[], bool] = lambda: False
    tally: Callable[[dict], None] = lambda usage: None
    llm_factory: Callable[[], BaseChatModel] | None = None
    window: int = 128_000
    parallel: int = 3
    sleep: Callable[[float], None] = time.sleep


_CTX: Context | None = None


def set_context(ctx: Context | None) -> None:
    global _CTX
    _CTX = ctx


def get_context() -> Context | None:
    return _CTX


class Stopped(Exception):
    pass


@dataclass
class Outcome:
    id: str
    label: str
    status: str                       # done | failed | stopped
    result: ResearchResult | None = None
    error: str | None = None
    usage: dict = field(default_factory=dict)
    seconds: float = 0.0


# ---- budgets ----------------------------------------------------------------------------------------


def budgeted(tool, limit: int | None):
    """The same tool, refusing after `limit` calls and saying what to do instead."""
    if not limit:
        return tool
    lock, n = threading.Lock(), [0]

    def run(**kwargs):
        with lock:
            n[0] += 1
            over = n[0] > limit
        if over:
            return (f"ERROR: you have used all {limit} {tool.name} calls you are allowed. Stop searching, finish with what "
                    f"you have verified, and list everything else as gaps.")
        return tool.invoke(kwargs)

    return StructuredTool.from_function(func=run, name=tool.name, description=tool.description, args_schema=tool.args_schema)


def remembering_dead_ends(tool):
    """web_fetch that does not knock on a closed door twice.

    An address that failed is not tried again, and a site that has refused us or been unreachable twice is left alone.
    Both answers come back at once and do not use the fetch budget, which is better spent on a page that can be read."""
    from urllib.parse import urlsplit

    lock, failed, hosts = threading.Lock(), {}, {}
    barred = ("answered 403", "answered 401", "could not reach", "timed out")

    def run(**kwargs):
        url = str(kwargs.get("url", "")).strip()
        host = (urlsplit(url).hostname or "").removeprefix("www.")
        with lock:
            if url in failed:
                return f"ERROR: you already tried this address and it failed ({failed[url]}). Try a different page, a PDF, or another publisher."
            if host and hosts.get(host, 0) >= 2:
                return (f"ERROR: {host} has refused or not answered twice, so it is not worth more of your budget. Look for the same figure "
                        f"on another publisher's page, or record it as a gap.")
        out = tool.invoke(kwargs)
        if isinstance(out, str) and out.startswith("ERROR"):
            why = out[6:].strip()[:80]
            with lock:
                failed[url] = why
                if host and any(b in out for b in barred):
                    hosts[host] = hosts.get(host, 0) + 1
        return out

    return StructuredTool.from_function(func=run, name=tool.name, description=tool.description, args_schema=tool.args_schema)


# ---- running ------------------------------------------------------------------------------------------


def _text(msg: AIMessage) -> str:
    c = msg.content
    return c if isinstance(c, str) else " ".join(b.get("text", "") for b in c if isinstance(b, dict))


def _stream(agent, cfg, payload, ctx: Context, sid: str, usage: dict) -> str | None:
    """Run the graph, narrating each step into the feed. Returns the final reply (None if it ran out of steps)."""
    final = None
    for chunk in agent.stream(payload, cfg, stream_mode="updates"):
        for node, update in chunk.items():
            if not isinstance(update, dict):
                continue
            for msg in update.get("messages") or []:
                if isinstance(msg, AIMessage):
                    u = getattr(msg, "usage_metadata", None)
                    if u:
                        usage["calls"] = usage.get("calls", 0) + 1
                        usage["input"] = usage.get("input", 0) + int(u.get("input_tokens") or 0)
                        usage["output"] = usage.get("output", 0) + int(u.get("output_tokens") or 0)
                        usage["cached"] = usage.get("cached", 0) + int((u.get("input_token_details") or {}).get("cache_read") or 0)
                        ctx.tally(u)
                    if msg.tool_calls:
                        for c in msg.tool_calls:
                            chapter, label = narrator.describe_call(c["name"], c.get("args") or {})
                            usage["steps"] = usage.get("steps", 0) + 1
                            ctx.emit("step", label, chapter=chapter,
                                     detail={"call": c["id"], "tool": c["name"], "status": "running", "sub": sid})
                    else:
                        t = _text(msg).strip()
                        final = None if t.startswith(OUT_OF_STEPS) else t
                elif isinstance(msg, ToolMessage):
                    level, text = narrator.describe_result(msg.name or "", msg.content, getattr(msg, "status", None))
                    ctx.emit("step.done", text or "", detail={"call": msg.tool_call_id, "tool": msg.name, "level": level, "sub": sid})
        if ctx.should_stop():
            raise Stopped()
    return final


def _ask(agent, cfg, message: str, ctx: Context, sid: str, usage: dict) -> str | None:
    return _stream(agent, {**cfg, "recursion_limit": 8}, {"messages": [HumanMessage(content=message)]}, ctx, sid, usage)


def run_subagent(spec: Subagent, task: str, *, label: str, ctx: Context, group: str | None = None) -> Outcome:
    sid = uuid.uuid4().hex[:10]
    t0 = time.time()
    usage: dict = {}
    records.start(ctx.store, sid, ctx, spec.name, label, task)
    ctx.emit("subagent.start", f"{spec.title} · {label}", detail={"sub": sid, "agent": spec.name, "label": label})
    status, error, result = "done", None, None
    try:
        if ctx.llm_factory is None:
            raise RuntimeError("no model is configured for subagents")
        tools = [remembering_dead_ends(budgeted(t, spec.budgets.get(t.name))) if t.name == "web_fetch"
                 else budgeted(t, spec.budgets.get(t.name)) for t in spec.tools]
        meter = ContextMeter(ctx.window, reserve=16_000, overhead=4_000)
        agent = create_react_agent(ctx.llm_factory(), tools=ToolNode(tools, handle_tool_errors=True), prompt=spec.prompt,
                                   checkpointer=InMemorySaver(), pre_model_hook=make_hook(meter))
        cfg = {"configurable": {"thread_id": sid}, "recursion_limit": spec.max_rounds * 2 + 2}
        text = _run_with_retries(agent, cfg, task, ctx, sid, usage)
        if text is None:                                         # out of steps: ask for the answer now, with no more tools
            text = _ask(agent, cfg, "You are out of steps. Reply now with the JSON result, using only what you have "
                                    "verified so far and listing everything else as gaps.", ctx, sid, usage)
        try:
            result = parse_result(text or "")
        except ValueError as exc:                                # one chance to put it in the required shape
            fixed = _ask(agent, cfg, f"Your reply could not be used ({exc}). Reply with only the JSON object in the required "
                                     f"format, nothing else.", ctx, sid, usage)
            result = parse_result(fixed or "")
    except Stopped:
        status = "stopped"
    except ValueError:
        status, error = "failed", "the researcher did not return a usable result"
    except Exception as exc:  # noqa: BLE001 - one subagent failing must never take the run down
        status, error = "failed", classify(exc).message
    seconds = time.time() - t0
    records.finish(ctx.store, sid, status, result.model_dump() if result else None, usage, error)
    n = len(result.claims) if result else 0
    ctx.emit("subagent.done", (f"{n} figure{'s' if n != 1 else ''} found" if status == "done" else (error or status)),
             detail={"sub": sid, "status": status, "usage": usage, "seconds": round(seconds, 1), "label": label})
    return Outcome(sid, label, status, result, error, usage, seconds)


def _run_with_retries(agent, cfg, task: str, ctx: Context, sid: str, usage: dict) -> str | None:
    attempt, payload = 0, {"messages": [HumanMessage(content=task)]}
    while True:
        try:
            return _stream(agent, cfg, payload, ctx, sid, usage)
        except Stopped:
            raise
        except Exception as exc:  # noqa: BLE001
            f = classify(exc)
            if f.kind == "step_limit":
                return None
            if not f.retry or attempt >= min(f.attempts, 3):
                raise
            wait = backoff(f, attempt, exc)
            attempt += 1
            ctx.emit("retry", f"{f.message} Trying again in {int(round(wait))} seconds.",
                     detail={"sub": sid, "attempt": attempt, "of": 3, "wait": wait, "kind": f.kind})
            end = time.time() + wait
            while time.time() < end:
                if ctx.should_stop():
                    raise Stopped() from exc
                ctx.sleep(min(1.0, max(0.0, end - time.time())))
            payload = None if agent.get_state(cfg).values.get("messages") else {"messages": [HumanMessage(content=task)]}
