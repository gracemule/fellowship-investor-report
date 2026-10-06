"""The database record of delegated work."""

from __future__ import annotations

from psycopg.types.json import Jsonb

from ..agent.store import Store


def start(store: Store, sid: str, ctx, agent: str, label: str, task: str) -> None:
    with store.conn() as c:
        c.execute(f"INSERT INTO {store._t('subagent_run')} (id, report_id, run_id, session_id, agent, label, task) "
                  f"VALUES (%s,%s,%s,%s,%s,%s,%s)", (sid, ctx.report_id, ctx.run_id, ctx.session_id, agent, label, task[:4000]))


def finish(store: Store, sid: str, status: str, result: dict | None, usage: dict, error: str | None) -> None:
    with store.conn() as c:
        c.execute(f"UPDATE {store._t('subagent_run')} SET status=%s, result=%s, usage=%s, error=%s, finished_at=now() WHERE id=%s",
                  (status, Jsonb(result) if result is not None else None, Jsonb(usage), error, sid))


def attach_recorded(store: Store, sid: str, recorded: dict) -> None:
    """What the code verified and recorded from the subagent's claims (the claims alone are not trusted)."""
    with store.conn() as c:
        c.execute(f"UPDATE {store._t('subagent_run')} SET result = coalesce(result,'{{}}'::jsonb) || %s WHERE id=%s",
                  (Jsonb({"recorded": recorded}), sid))


def latest_done(store: Store, report_id: str, agent: str, labels: list[str]) -> dict[str, dict]:
    """The most recent finished run for each label."""
    with store.conn() as c:
        rows = list(c.execute(
            f"SELECT DISTINCT ON (label) id, label, result, usage, finished_at FROM {store._t('subagent_run')} "
            f"WHERE report_id=%s AND agent=%s AND status='done' AND label = ANY(%s) ORDER BY label, started_at DESC",
            (report_id, agent, labels)))
    return {r["label"]: r for r in rows}


def interrupt_running(store: Store, older_than_seconds: int = 0) -> int:
    """After a restart, work that was in flight cannot be resumed; say so rather than leave it 'running'."""
    with store.conn() as c:
        return c.execute(f"UPDATE {store._t('subagent_run')} SET status='interrupted', finished_at=now() "
                         f"WHERE status='running' AND started_at < now() - make_interval(secs => %s) RETURNING id",
                         (older_than_seconds,)).rowcount


def all_done(store: Store, report_id: str, agent: str, labels: list[str]) -> dict[str, list[dict]]:
    """Every finished run for each label, newest first."""
    with store.conn() as c:
        rows = list(c.execute(
            f"SELECT id, label, result, usage, started_at FROM {store._t('subagent_run')} "
            f"WHERE report_id=%s AND agent=%s AND status='done' AND label = ANY(%s) ORDER BY label, started_at DESC",
            (report_id, agent, labels)))
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["label"], []).append(r)
    return out
