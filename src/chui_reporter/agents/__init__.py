"""Subagents: bulky work done in a context of its own.

A researcher that reads fifty web pages would fill the main agent's conversation with page text it will never
need again. A subagent does that work in a separate, disposable conversation and hands back a page of verified
results; the pages never enter the main context. This is the pattern Anthropic describes for multi-agent
research (subagents return 1,000 to 2,000 tokens after using tens of thousands) and the one coding agents such as
Copilot and Cline use for the same reason.

What is specific here:

* Subagents are *workers, not authors*. They find and quote; they cannot write into the report. Every figure
  they claim is checked by code against a stored copy of the page before it enters the fact ledger, so the
  gate protects the report exactly as it does for the main agent.
* They cannot ask the user, cannot start further subagents, and have a hard budget of steps and searches (web
  search credit is shared and limited).
* They run in parallel, narrate into the same activity feed (nested under the step that started them), and
  report what they cost. A subagent that fails costs the main agent one gap, not the run.
"""
