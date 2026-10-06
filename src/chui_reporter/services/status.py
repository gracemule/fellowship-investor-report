"""What the outside services are doing, for the interface: web search providers and the PDF converter.

Built from rows the page-state query already fetched, so showing it costs no extra database round trips."""

from __future__ import annotations

import os
from datetime import datetime, timezone


def build(state_rows: list[dict], usage_rows: list[dict]) -> dict:
    from ..render import converters

    states = {r["provider"]: r for r in state_rows}
    used = {r["provider"]: r["calls"] for r in usage_rows}
    now = datetime.now(timezone.utc).isoformat()

    def provider(name: str, key_env: str) -> dict:
        st = states.get(name)
        active = bool(st) and st["state"] != "ok" and (st.get("until") is None or str(st["until"]) > now)
        configured = bool(os.environ.get(key_env, "").strip())
        return {"name": name, "configured": configured, "used": used.get(name, 0),
                "state": "unset" if not configured else (st["state"] if active else "ok"),
                "detail": (st.get("detail") or "") if active else ""}

    order = [x.strip().lower() for x in os.environ.get("CHUI_SEARCH_ORDER", "tavily,brave").split(",")]
    keys = {"tavily": "TAVILY_API_KEY", "brave": "BRAVE_API_KEY"}
    search = [provider(n, keys[n]) for n in order if n in keys] or [provider(n, k) for n, k in keys.items()]
    conv = converters.describe()
    ist = states.get("iloveapi")
    conv["used"] = used.get("iloveapi", 0)
    conv["detail"] = (ist or {}).get("detail", "") if conv["name"] == "iloveapi" else ""
    return {"search": search, "converter": conv}
