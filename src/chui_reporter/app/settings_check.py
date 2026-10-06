"""Which settings does this server see? Names and whether each is set, never values.

Render does not show a service's environment to anyone but its owner, and a missing or mistyped variable looks the same as a
wrong one. This puts one plain line in the log at start-up (and in the error if the password is missing) so a typo, an empty box
or a value saved in the wrong service is visible at once."""

from __future__ import annotations

import os

REQUIRED = ("CHUI_ACCESS_PASSWORD", "DATABASE_URL")
EXPECTED = REQUIRED + ("DEEPSEEK_API_KEY", "TAVILY_API_KEY", "BRAVE_API_KEY", "ANTHROPIC_API_KEY", "CHUI_CONVERTER_URL",
                       "CHUI_CONVERTER_TOKEN", "CHUI_PDF_CONVERTER", "CHUI_PROVIDER", "CHUI_ENV", "CHUI_SESSION_SECRET")


def report(environ=None) -> str:
    env = os.environ if environ is None else environ
    parts = []
    for name in EXPECTED:
        v = env.get(name)
        parts.append(f"{name}=" + ("MISSING" if v is None else "EMPTY" if not v.strip() else f"set({len(v.strip())} chars)"))
    # Anything that looks like it was meant to be one of the above but is not spelled the same.
    extra = sorted(k for k in env if k not in EXPECTED and (k.startswith("CHUI_") or "PASSWORD" in k.upper() or "PASSWD" in k.upper())
                   and k not in ("CHUI_WORKDIR", "CHUI_SEARCH_ORDER", "CHUI_FALLBACK_PROVIDER", "CHUI_JANITOR_SECONDS",
                                 "CHUI_CONTEXT_FRACTION", "CHUI_CONTEXT_DISCARD_FRACTION", "CHUI_CONTEXT_BUDGET",
                                 "CHUI_SUBAGENT_PARALLEL", "CHUI_SUBAGENT_MODEL", "CHUI_PERIOD", "CHUI_THINKING",
                                 "CHUI_EMBED_FONTS", "CHUI_SOURCE_ROOT"))
    line = "settings seen by this server: " + ", ".join(parts)
    if extra:
        line += " | other variables that may be misspelled: " + ", ".join(extra)
    return line
