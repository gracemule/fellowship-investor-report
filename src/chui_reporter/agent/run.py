"""CLI for the reporting agent.

    python -m chui_reporter.agent.run "Build the Q2 2026 portfolio section"
    python -m chui_reporter.agent.run --thread q2-2026 --resume
    python -m chui_reporter.agent.run --thread q2-2026 "Redo 1.1, it is too long"

Narration is streamed as the agent works: each tool call and each result is
printed as it happens, which is the terminal equivalent of the run timeline the
console will show.
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import find_dotenv, load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from .graph import build_agent, build_checkpointer
from .llm import ProviderError, describe, get_llm, require_key, resolve_provider


def repair_dangling_tool_calls(agent, config) -> int:
    """Restore a valid message history after an interruption, and return how many messages
    it had to synthesise or move.

    Providers require every tool call to be answered by a tool message that comes
    *directly* after the call. A crash or kill mid-call leaves a call with no answer; a new
    instruction sent on top of that leaves the answer (if added later) in the wrong place.
    Either way the thread cannot be continued. So the history is rebuilt in order: each call
    is followed by its own result, with an explicit 'interrupted' result where none exists.
    """
    from langchain_core.messages import RemoveMessage
    from langgraph.graph.message import REMOVE_ALL_MESSAGES

    state = agent.get_state(config)
    if any(t.interrupts for t in (state.tasks or ())):
        return 0                       # parked on a question: that call is waiting, not dangling
    msgs = list((state.values or {}).get("messages", []))
    by_call = {m.tool_call_id: m for m in msgs if isinstance(m, ToolMessage)}
    rebuilt, used, synthesised = [], set(), 0
    for m in msgs:
        if isinstance(m, ToolMessage):
            continue                       # re-emitted directly after its own call, below
        rebuilt.append(m)
        if isinstance(m, AIMessage):
            # A call whose arguments were cut off or malformed is recorded as an *invalid* call,
            # but is still sent to the provider as a tool call, so it needs an answer too.
            invalid = [{"id": c.get("id"), "name": c.get("name") or "tool", "invalid": True}
                       for c in (getattr(m, "invalid_tool_calls", None) or []) if c.get("id")]
            for tc in list(m.tool_calls or []) + invalid:
                if tc["id"] in by_call:
                    rebuilt.append(by_call[tc["id"]])
                    used.add(tc["id"])
                elif tc.get("invalid"):
                    rebuilt.append(ToolMessage(
                        content="Your arguments for this call were cut off or were not valid JSON, so "
                                "nothing ran. Send it again in smaller pieces (for report_save_facts, at "
                                "most 10 facts per call).",
                        tool_call_id=tc["id"], name=tc["name"], status="error"))
                    synthesised += 1
                else:
                    rebuilt.append(ToolMessage(
                        content="This call was interrupted before it ran, so it has no result. "
                                "Continue with the task and call it again only if it is still needed.",
                        tool_call_id=tc["id"], name=tc["name"], status="error"))
                    synthesised += 1
    # Tool messages answering a call that no longer exists are dropped (also invalid).
    moved = sum(1 for a, b in zip(msgs, rebuilt) if a is not b) + abs(len(msgs) - len(rebuilt))
    if synthesised == 0 and moved == 0:
        return 0
    agent.update_state(config, {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *rebuilt]},
                       as_node="tools")
    return max(synthesised, 1)


def unfinished(store) -> list[str]:
    """What is still outstanding before the work can be called done."""
    c = store.completion()
    missing = []
    if not c["rendered"]:
        missing.append("render the report with report_render (it has not been rendered since your last change)")
    if not c["inspected"]:
        missing.append("look at the rendered pages with inspect_pages (cover and logo first), fix what you "
                       "can, and re-render if you changed anything")
    return missing


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="chui-agent")
    ap.add_argument("prompt", nargs="*", help="instruction for the agent")
    ap.add_argument("--thread", default="default",
                    help="thread id; reuse it to continue or steer a run")
    ap.add_argument("--resume", action="store_true",
                    help="continue a parked run without a new instruction")
    ap.add_argument("--provider", default=None, choices=["deepseek", "anthropic"],
                    help="LLM provider (default: $CHUI_PROVIDER, else deepseek)")
    ap.add_argument("--model", default=None, help="override the provider's default model")
    ap.add_argument("--thinking", choices=["on", "off"], default=None,
                    help="DeepSeek thinking mode (default: off; see llm.py)")
    ap.add_argument("--no-supervise", dest="supervise", action="store_false",
                    help="do not send the agent back to work if it stops before rendering and inspecting")
    ap.add_argument("--max-nudges", type=int, default=6)
    ap.add_argument("--check", action="store_true",
                    help="verify the key and tool-calling against the provider, then exit")
    ap.add_argument("--max-steps", type=int, default=60)
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)  # progress must be visible when piped

    # .env is found by walking up from the working directory; real environment
    # variables win over the file.
    load_dotenv(find_dotenv(usecwd=True), override=False)

    thinking = None if args.thinking is None else args.thinking == "on"
    try:
        require_key(resolve_provider(args.provider))
    except ProviderError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.check:
        return _check(args.provider, args.model, thinking)

    checkpointer, cm = build_checkpointer()
    try:
        agent = build_agent(checkpointer, args.provider, args.model, thinking)
        config = {"configurable": {"thread_id": args.thread},
                  "recursion_limit": args.max_steps}

        text = " ".join(args.prompt).strip()
        if args.resume and not text:
            state = agent.get_state(config)
            if not state.values:
                print(f"no state for thread {args.thread!r}", file=sys.stderr)
                return 1
            payload = None
        else:
            if not text:
                ap.error("give an instruction, or use --resume on an existing thread")
            payload = {"messages": [HumanMessage(content=text)]}
            fixed = repair_dangling_tool_calls(agent, config)
            if fixed:
                print(f"[thread {args.thread}] repaired the interrupted history before continuing")
            history = (agent.get_state(config).values or {}).get("messages", [])
            if history and isinstance(history[-1], HumanMessage) and history[-1].content == text:
                payload = None             # a failed attempt already recorded this instruction

        print(f"[{describe(args.provider, args.model, thinking)}] "
              f"[thread {args.thread}] {'resuming' if payload is None else text}\n")
        from .tools import get_store

        nudges = 0
        try:
            while True:
                for chunk in agent.stream(payload, config, stream_mode="updates"):
                    for node, update in chunk.items():
                        for msg in update.get("messages", []):
                            _narrate(node, msg)
                # The model ends a turn whenever it answers in text. That is not the same
                # as the work being finished, so check, and send it back to work if not.
                missing = unfinished(get_store()) if args.supervise else []
                if not missing or nudges >= args.max_nudges:
                    if missing:
                        print(f"\n[thread {args.thread}] stopped with work outstanding: "
                              + "; ".join(missing), file=sys.stderr)
                    break
                nudges += 1
                print(f"\n[thread {args.thread}] not finished -- sending it back to work "
                      f"({nudges}/{args.max_nudges}): {'; '.join(missing)}")
                repair_dangling_tool_calls(agent, config)
                payload = {"messages": [HumanMessage(content=(
                    "You have not finished. Still to do: " + "; ".join(missing) + ". Do it now by calling "
                    "the tools. Do not describe what you are about to do; do it."))]}
        except Exception as exc:  # noqa: BLE001
            print(f"\n[thread {args.thread}] STOPPED: {type(exc).__name__}: {exc}\n"
                  f"State is saved; fix the cause and continue with "
                  f"--thread {args.thread} --resume", file=sys.stderr)
            return 1
        print(f"\n[thread {args.thread}] done. Resume or steer with --thread {args.thread}")
        return 0
    finally:
        cm.__exit__(None, None, None)


def _check(provider, model, thinking) -> int:
    """One cheap round trip that proves the key works AND tool calling works --
    the second is what the agent actually depends on."""
    from langchain_core.tools import tool

    @tool
    def ping(word: str) -> str:
        """Echo a word back."""
        return word

    label = describe(provider, model, thinking)
    try:
        llm = get_llm(provider, model, thinking=thinking).bind_tools([ping])
        reply = llm.invoke("Call the ping tool with the word 'ok'.")
    except Exception as exc:  # noqa: BLE001 - surface the provider's own error
        print(f"[{label}] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    calls = getattr(reply, "tool_calls", None) or []
    if not calls:
        print(f"[{label}] key accepted but the model did not call the tool; "
              f"the agent will not work with this model/mode.", file=sys.stderr)
        return 1
    print(f"[{label}] OK: key accepted, tool call returned ({calls[0]['name']}).")
    return 0


def _narrate(node: str, msg) -> None:
    calls = getattr(msg, "tool_calls", None)
    if calls:
        for c in calls:
            arg = ", ".join(f"{k}={str(v)[:50]}" for k, v in (c.get("args") or {}).items())
            print(f"  -> {c['name']}({arg})")
        return
    if msg.__class__.__name__ == "ToolMessage":
        body = str(msg.content)
        first = body.splitlines()[0] if body else ""
        extra = len(body.splitlines()) - 1
        print(f"     {first[:150]}" + (f"  (+{extra} more lines)" if extra > 0 else ""))
        return
    if getattr(msg, "content", None):
        content = msg.content
        if isinstance(content, list):
            content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
        if str(content).strip():
            print(f"\n{str(content).strip()}\n")


if __name__ == "__main__":
    raise SystemExit(main())
