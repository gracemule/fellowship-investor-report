"""Usage and availability of the outside services the agent calls (web search, PDF conversion).

Kept in the database, not memory, so a restart does not forget that a provider ran out of credit, and
so the interface can show what has been used this month."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..agent.store import Store


def month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def record(store: Store, provider: str, n: int = 1) -> None:
    with store.conn() as c:
        c.execute(f"INSERT INTO {store._t('service_usage')} (provider, month, calls) VALUES (%s,%s,%s) "
                  f"ON CONFLICT (provider, month) DO UPDATE SET calls = {store._t('service_usage')}.calls + EXCLUDED.calls",
                  (provider, month(), n))


def used_this_month(store: Store, provider: str) -> int:
    with store.conn() as c:
        r = c.execute(f"SELECT calls FROM {store._t('service_usage')} WHERE provider=%s AND month=%s",
                      (provider, month())).fetchone()
    return r["calls"] if r else 0


def get_state(store: Store, provider: str) -> dict:
    with store.conn() as c:
        r = c.execute(f"SELECT state, until, detail FROM {store._t('service_state')} WHERE provider=%s",
                      (provider,)).fetchone()
    if not r:
        return {"state": "ok", "until": None, "detail": ""}
    until = r["until"]
    if r["state"] != "ok" and until is not None and until <= datetime.now(timezone.utc):
        return {"state": "ok", "until": None, "detail": "cooldown over; trying again"}
    return {"state": r["state"], "until": until.isoformat() if until else None, "detail": r["detail"] or ""}


def set_state(store: Store, provider: str, state: str, *, hours: float = 0, detail: str = "") -> None:
    until = datetime.now(timezone.utc) + timedelta(hours=hours) if hours else None
    with store.conn() as c:
        c.execute(f"INSERT INTO {store._t('service_state')} (provider, state, until, detail, updated_at) "
                  f"VALUES (%s,%s,%s,%s,now()) ON CONFLICT (provider) DO UPDATE SET state=EXCLUDED.state, "
                  f"until=EXCLUDED.until, detail=EXCLUDED.detail, updated_at=now()", (provider, state, until, detail))


def clear_state(store: Store, provider: str) -> None:
    set_state(store, provider, "ok")
