"""Run, event, question and version records.

Everything the interface shows comes from here, not from the agent process: the agent can
crash, the server can restart, the browser tab can be closed for a week, and the page still
reopens exactly where the work stands. The event table is the single ordered record the UI
replays from any point.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from ..agent.store import Store

WS = "default"
ACTIVE = ("queued", "running", "waiting_user", "waiting_data")


def _iso(row: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}


# ---- workspace ----------------------------------------------------------------------------


def workspace(store: Store, ws: str = WS) -> dict:
    with store.conn() as c:
        from .. import period as pr
        c.execute(f"INSERT INTO {store._t('workspace')} (id, period) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                  (ws, pr.current().code))
        row = c.execute(f"SELECT id, name, period, last_sync_at, settings FROM {store._t('workspace')} "
                        f"WHERE id=%s", (ws,)).fetchone()
    return _iso(row)


def update_workspace(store: Store, ws: str = WS, *, period: str | None = None, name: str | None = None,
                     settings: dict | None = None) -> dict:
    workspace(store, ws)
    with store.conn() as c:
        if period is not None:
            c.execute(f"UPDATE {store._t('workspace')} SET period=%s WHERE id=%s", (period, ws))
        if name is not None:
            c.execute(f"UPDATE {store._t('workspace')} SET name=%s WHERE id=%s", (name, ws))
        if settings:
            c.execute(f"UPDATE {store._t('workspace')} SET settings = settings || %s WHERE id=%s",
                      (Jsonb(settings), ws))
    return workspace(store, ws)


# ---- events -------------------------------------------------------------------------------


_session_provider = None


def set_session_provider(fn) -> None:
    """The runtime tells this module which session new events belong to (None = no sessions)."""
    global _session_provider
    _session_provider = fn


def emit(store: Store, kind: str, label: str = "", *, run_id: str | None = None,
         chapter: str | None = None, detail: dict | None = None, ws: str = WS,
         session_id: str | None = None) -> int:
    if session_id is None and _session_provider is not None:
        try:
            session_id = _session_provider()
        except Exception:               # noqa: BLE001 - an event is never lost for want of a session
            session_id = None
    with store.conn() as c:
        row = c.execute(
            f"INSERT INTO {store._t('event')} (workspace_id, run_id, kind, label, chapter, detail, session_id) "
            f"VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (ws, run_id, kind, label[:600], chapter, Jsonb(detail or {}), session_id)).fetchone()
    return row["id"]


def events_after(store: Store, after: int = 0, limit: int = 300, ws: str = WS) -> list[dict]:
    with store.conn() as c:
        rows = list(c.execute(
            f"SELECT id, run_id, kind, label, chapter, detail, created_at FROM {store._t('event')} "
            f"WHERE workspace_id=%s AND id>%s ORDER BY id LIMIT %s", (ws, after, limit)))
    return [_iso(r) for r in rows]


def recent_events(store: Store, n: int = 120, ws: str = WS, *, session: str | None = None,
                  include_unassigned: bool = False) -> list[dict]:
    """The last `n` events; of one session if given (events from before sessions existed belong to the
    oldest session, `include_unassigned`)."""
    where, args = "workspace_id=%s", [ws]
    if session:
        where += " AND (session_id=%s" + (" OR session_id IS NULL" if include_unassigned else "") + ")"
        args.append(session)
    with store.conn() as c:
        rows = list(c.execute(
            f"SELECT id, run_id, kind, label, chapter, detail, created_at, session_id FROM {store._t('event')} "
            f"WHERE {where} ORDER BY id DESC LIMIT %s", (*args, n)))
    return [_iso(r) for r in reversed(rows)]


def last_event_id(store: Store, ws: str = WS) -> int:
    with store.conn() as c:
        r = c.execute(f"SELECT coalesce(max(id),0) AS m FROM {store._t('event')} WHERE workspace_id=%s",
                      (ws,)).fetchone()
    return r["m"]


# ---- runs ---------------------------------------------------------------------------------


def create_run(store: Store, kind: str, instruction: str, thread_id: str, ws: str = WS,
               session_id: str | None = None) -> str:
    rid = uuid.uuid4().hex[:12]
    with store.conn() as c:
        c.execute(f"INSERT INTO {store._t('run')} (id, workspace_id, thread_id, kind, instruction, status, session_id) "
                  f"VALUES (%s,%s,%s,%s,%s,'queued',%s)", (rid, ws, thread_id, kind, instruction, session_id))
    return rid


def get_run(store: Store, run_id: str) -> dict | None:
    with store.conn() as c:
        r = c.execute(f"SELECT * FROM {store._t('run')} WHERE id=%s", (run_id,)).fetchone()
    return _iso(r) if r else None


def active_run(store: Store, ws: str = WS) -> dict | None:
    with store.conn() as c:
        r = c.execute(f"SELECT * FROM {store._t('run')} WHERE workspace_id=%s AND status = ANY(%s) "
                      f"ORDER BY created_at DESC LIMIT 1", (ws, list(ACTIVE))).fetchone()
    return _iso(r) if r else None


def latest_run(store: Store, ws: str = WS) -> dict | None:
    with store.conn() as c:
        r = c.execute(f"SELECT * FROM {store._t('run')} WHERE workspace_id=%s "
                      f"ORDER BY created_at DESC LIMIT 1", (ws,)).fetchone()
    return _iso(r) if r else None


_RUN_FIELDS = {"status", "error", "worker", "attempts", "nudges", "instruction"}


def update_run(store: Store, run_id: str, **fields: Any) -> None:
    bad = set(fields) - _RUN_FIELDS
    if bad:
        raise ValueError(f"cannot set {sorted(bad)} on a run")
    if not fields:
        return
    sets = ", ".join(f"{k}=%s" for k in fields)
    with store.conn() as c:
        c.execute(f"UPDATE {store._t('run')} SET {sets}, updated_at=now(), heartbeat_at=now() WHERE id=%s",
                  (*fields.values(), run_id))


def touch(store: Store, run_id: str) -> None:
    with store.conn() as c:
        c.execute(f"UPDATE {store._t('run')} SET heartbeat_at=now() WHERE id=%s", (run_id,))


def claim(store: Store, run_id: str, worker: str) -> bool:
    """Atomically take a queued run. Two workers cannot both start it."""
    with store.conn() as c:
        r = c.execute(f"UPDATE {store._t('run')} SET status='running', worker=%s, attempts=attempts+1, "
                      f"updated_at=now(), heartbeat_at=now() WHERE id=%s AND status='queued' RETURNING id",
                      (worker, run_id)).fetchone()
    return bool(r)


def stale_runs(store: Store, seconds: int = 75, ws: str = WS) -> list[dict]:
    """Runs marked running whose worker has stopped reporting: the process died."""
    with store.conn() as c:
        rows = list(c.execute(
            f"SELECT * FROM {store._t('run')} WHERE workspace_id=%s AND status='running' "
            f"AND coalesce(heartbeat_at, updated_at) < now() - make_interval(secs => %s)", (ws, seconds)))
    return [_iso(r) for r in rows]


def requeue(store: Store, run_id: str) -> None:
    with store.conn() as c:
        c.execute(f"UPDATE {store._t('run')} SET status='queued', worker=NULL, updated_at=now() "
                  f"WHERE id=%s AND status IN ('running','waiting_user','waiting_data')", (run_id,))


# ---- questions ----------------------------------------------------------------------------


def open_question(store: Store, qid: str, run_id: str, kind: str, prompt: str, why: str = "",
                  options: list[str] | None = None, slots: list[str] | None = None) -> str:
    """Idempotent on `qid` (the interrupt's own id): a resumed node reports the same pause again."""
    with store.conn() as c:
        c.execute(
            f"INSERT INTO {store._t('question')} (id, run_id, kind, prompt, why, options, slots) "
            f"VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
            (qid, run_id, kind, prompt, why, Jsonb(options or []), Jsonb(slots or [])))
    return qid


def get_question(store: Store, qid: str) -> dict | None:
    with store.conn() as c:
        r = c.execute(f"SELECT * FROM {store._t('question')} WHERE id=%s", (qid,)).fetchone()
    return _iso(r) if r else None


def open_questions(store: Store, run_id: str | None = None) -> list[dict]:
    with store.conn() as c:
        if run_id:
            rows = list(c.execute(f"SELECT * FROM {store._t('question')} WHERE status='open' AND run_id=%s "
                                  f"ORDER BY created_at", (run_id,)))
        else:
            rows = list(c.execute(f"SELECT * FROM {store._t('question')} WHERE status='open' "
                                  f"ORDER BY created_at"))
    return [_iso(r) for r in rows]


def answer_question(store: Store, qid: str, answer: str) -> dict | None:
    """Record an answer. Returns the question, or None if it was not open (already answered)."""
    with store.conn() as c:
        r = c.execute(f"UPDATE {store._t('question')} SET status='answered', answer=%s, answered_at=now() "
                      f"WHERE id=%s AND status='open' RETURNING *", (answer, qid)).fetchone()
    return _iso(r) if r else None


# ---- report versions ----------------------------------------------------------------------


def save_version(store: Store, pages: int, summary: dict, pdf: bytes, docx: bytes | None,
                 notes: str | None) -> int:
    with store.conn() as c:
        n = c.execute(f"SELECT coalesce(max(version),0)+1 AS n FROM {store._t('report_version')} "
                      f"WHERE report_id=%s", (store.report_id,)).fetchone()["n"]
        c.execute(f"INSERT INTO {store._t('report_version')} (report_id, version, pages, summary, pdf, docx, notes) "
                  f"VALUES (%s,%s,%s,%s,%s,%s,%s)",
                  (store.report_id, n, pages, Jsonb(summary), pdf, docx, notes))
    return n


def versions(store: Store, limit: int = 20) -> list[dict]:
    with store.conn() as c:
        rows = list(c.execute(
            f"SELECT version, pages, summary, created_at FROM {store._t('report_version')} "
            f"WHERE report_id=%s ORDER BY version DESC LIMIT %s", (store.report_id, limit)))
    return [_iso(r) for r in rows]


def latest_version(store: Store) -> dict | None:
    v = versions(store, 1)
    return v[0] if v else None


_BLOBS: dict[tuple, bytes] = {}


def version_blob(store: Store, version: int, which: str) -> bytes | None:
    """A stored file of a version. A version never changes, so recent ones are kept in memory:
    the viewer asks for the same PDF once per page."""
    if which not in ("pdf", "docx"):
        raise ValueError(which)
    key = (store.schema, store.report_id, version, which)
    if key in _BLOBS:
        return _BLOBS[key]
    with store.conn() as c:
        r = c.execute(f"SELECT {which} AS b FROM {store._t('report_version')} WHERE report_id=%s AND version=%s",
                      (store.report_id, version)).fetchone()
    blob = bytes(r["b"]) if r and r["b"] is not None else None
    if blob is not None:
        _BLOBS[key] = blob
        while len(_BLOBS) > 4:
            _BLOBS.pop(next(iter(_BLOBS)))
    return blob


# ---- one-query snapshot -------------------------------------------------------------------


def snapshot_rows(store: Store, ws: str = WS) -> dict:
    """Everything the interface needs in a single round trip.

    The database is a network hop away (and on a free tier, a long one), so building the
    page state from a dozen small queries would make every refresh slow. This reads it all at once.
    """
    t = store._t
    sql = f"""
    SELECT
      (SELECT to_jsonb(w) FROM {t('workspace')} w WHERE w.id=%(ws)s) AS workspace,
      coalesce((SELECT jsonb_agg(path ORDER BY path) FROM {t('source_file')}
                WHERE workspace_id=%(ws)s AND status='present'), '[]'::jsonb) AS paths,
      (SELECT to_jsonb(r) FROM (SELECT * FROM {t('run')} WHERE workspace_id=%(ws)s
                                ORDER BY (status = ANY(%(active)s)) DESC, created_at DESC LIMIT 1) r) AS run,
      coalesce((SELECT jsonb_agg(to_jsonb(q) ORDER BY q.created_at) FROM {t('question')} q
                WHERE q.status='open'), '[]'::jsonb) AS questions,
      coalesce((SELECT jsonb_agg(to_jsonb(v) ORDER BY v.version DESC) FROM
                (SELECT version, pages, summary, created_at FROM {t('report_version')}
                 WHERE report_id = 'fund-i-' || (SELECT period FROM {t('workspace')} WHERE id=%(ws)s)
                 ORDER BY version DESC LIMIT 8) v), '[]'::jsonb) AS versions,
      (SELECT coalesce(max(id),0) FROM {t('event')} WHERE workspace_id=%(ws)s) AS last_event,
      coalesce((SELECT jsonb_agg(to_jsonb(x)) FROM {t('service_state')} x), '[]'::jsonb) AS service_state,
      coalesce((SELECT jsonb_agg(to_jsonb(u)) FROM {t('service_usage')} u WHERE u.month=%(month)s), '[]'::jsonb) AS service_usage
    """
    with store.conn() as c:
        from ..services import usage as _usage
        args = {"ws": ws, "active": list(ACTIVE), "month": _usage.month()}
        row = c.execute(sql, args).fetchone()
        if row["workspace"] is None:
            c.execute(f"INSERT INTO {t('workspace')} (id) VALUES (%s) ON CONFLICT DO NOTHING", (ws,))
            row = c.execute(sql, args).fetchone()
    return row


# ---- sessions -------------------------------------------------------------------------------------


def sessions(store: Store, ws: str = WS) -> list[dict]:
    """Every session, newest first, each with a title taken from what was asked in it."""
    with store.conn() as c:
        rows = list(c.execute(f"SELECT id, period, thread_id, created_at FROM {store._t('session')} "
                              f"WHERE workspace_id=%s ORDER BY created_at DESC", (ws,)))
        evs = list(c.execute(
            f"SELECT session_id, kind, label, detail, created_at FROM {store._t('event')} WHERE workspace_id=%s "
            f"AND kind IN ('steer','run.start','version','run.end') ORDER BY id", (ws,)))
    oldest = rows[-1]["id"] if rows else None
    out = []
    for r in rows:
        mine = [e for e in evs if e["session_id"] == r["id"] or (e["session_id"] is None and r["id"] == oldest)]
        steer = next((e for e in mine if e["kind"] == "steer"), None)
        start = next((e for e in mine if e["kind"] == "run.start"), None)
        if steer:
            title = steer["label"]
        elif start:
            title = {"build": "Built the report", "update": "Updated the report"}.get((start["detail"] or {}).get("kind"), "Worked on the report")
        else:
            title = "New session"
        runs = sum(1 for e in mine if e["kind"] == "run.start")
        last = max([e["created_at"] for e in mine], default=r["created_at"])
        out.append({"id": r["id"], "period": r["period"], "title": title[:90], "runs": runs,
                    "created_at": r["created_at"].isoformat(), "last_at": last.isoformat()})
    return out


def current_session(store: Store, period: str, ws: str = WS) -> dict:
    """The session new work goes into. The first one adopts the conversation thread that existed before
    sessions did, so nothing already done is orphaned."""
    with store.conn() as c:
        r = c.execute(f"SELECT id, period, thread_id, created_at FROM {store._t('session')} "
                      f"WHERE workspace_id=%s AND period=%s ORDER BY created_at DESC LIMIT 1", (ws, period)).fetchone()
        if r:
            return _iso(r)
        sid = uuid.uuid4().hex[:10]
        r = c.execute(f"INSERT INTO {store._t('session')} (id, workspace_id, period, thread_id) VALUES (%s,%s,%s,%s) "
                      f"RETURNING id, period, thread_id, created_at", (sid, ws, period, f"{ws}-{period}")).fetchone()
    return _iso(r)


def create_session(store: Store, period: str, ws: str = WS) -> dict:
    sid = uuid.uuid4().hex[:10]
    with store.conn() as c:
        r = c.execute(f"INSERT INTO {store._t('session')} (id, workspace_id, period, thread_id) VALUES (%s,%s,%s,%s) "
                      f"RETURNING id, period, thread_id, created_at", (sid, ws, period, f"{ws}-{period}-{sid}")).fetchone()
    return _iso(r)


def oldest_session_id(store: Store, ws: str = WS) -> str | None:
    with store.conn() as c:
        r = c.execute(f"SELECT id FROM {store._t('session')} WHERE workspace_id=%s ORDER BY created_at LIMIT 1", (ws,)).fetchone()
    return r["id"] if r else None
