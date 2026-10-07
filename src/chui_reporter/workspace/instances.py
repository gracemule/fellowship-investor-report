"""Instances of a quarter: start again without losing what was done.

Everything the agent does for a quarter (its files, report and versions, figures, review notes, runs, activity and conversations)
belongs to one *instance* of that quarter. The open instance is stored under the quarter's own code (`2026Q2`), so every other part
of the system sees nothing new. Starting a new instance *parks* the open one under `2026Q2~1` (or `~2`) and the quarter opens blank;
opening a parked one swaps it back. A quarter holds at most CAP instances (the open one included): every instance carries its own
copy of the files and report versions, and the database is small.

Parked instances are inert. They are not read by the page, the agent or the retention job until they are opened again. They have
their own conversation threads, so a new instance never inherits what the agent remembered in another.
"""

from __future__ import annotations

from ..agent.store import Store

CAP = 3
SLOTS = tuple(range(1, CAP))        # parked instances live in slots 1 and 2; the open one is slot 0
REPORT_TABLES = ("section", "tbl", "chart", "fact", "review_note", "report_version", "subagent_run")


class InstanceError(RuntimeError):
    """Something the user can fix: the message is written for them."""


def key(code: str, slot: int = 0) -> str:
    return code if not slot else f"{code}~{slot}"


def _holds_many(c, t, keys: list[str]) -> dict[str, dict]:
    """What each of these instances holds, and when it started and was last worked on: one round trip for all of them (the
    database is a network hop away, so the number of statements is what makes this slow or fast)."""
    sess = f"SELECT id FROM {t('session')} WHERE period=k"
    rows = c.execute(f"""
      SELECT k AS key,
        (SELECT count(*) FROM {t('source_file')} WHERE workspace_id=k AND status='present') AS files,
        (SELECT count(*) FROM {t('report_version')} WHERE report_id='fund-i-'||k) AS versions,
        (SELECT count(*) FROM {t('review_note')} WHERE report_id='fund-i-'||k) AS notes,
        (SELECT count(*) FROM {t('section')} WHERE report_id='fund-i-'||k) AS sections,
        (SELECT count(*) FROM {t('fact')} WHERE report_id='fund-i-'||k) AS facts,
        (SELECT count(*) FROM {t('run')} WHERE period=k) AS runs,
        (SELECT count(*) FROM {t('session')} WHERE period=k) AS conversations,
        (SELECT count(*) FROM {t('event')} WHERE kind='steer' AND session_id IN ({sess})) AS messages,
        least((SELECT min(created_at) FROM {t('session')} WHERE period=k),
              (SELECT min(first_seen) FROM {t('source_file')} WHERE workspace_id=k)) AS started,
        greatest((SELECT max(updated_at) FROM {t('run')} WHERE period=k),
                 (SELECT max(created_at) FROM {t('report_version')} WHERE report_id='fund-i-'||k),
                 (SELECT max(updated_at) FROM {t('source_file')} WHERE workspace_id=k)) AS last
      FROM unnest(%s::text[]) AS k""", (keys,)).fetchall()
    out = {}
    for r in rows:
        h = {n: r[n] for n in ("files", "versions", "notes", "sections", "facts", "runs", "conversations", "messages")}
        h["started_at"] = r["started"].isoformat() if r["started"] else None
        h["last_at"] = r["last"].isoformat() if r["last"] else None
        # blank = nothing the agent or the user made (the feed's own lines do not count)
        h["empty"] = not any(h[n] for n in ("files", "versions", "notes", "sections", "facts", "runs", "messages"))
        out[r["key"]] = h
    return out


def _holds(c, t, k: str) -> dict:
    return _holds_many(c, t, [k])[k]


def _purge(c, t, k: str) -> list[str]:
    """Delete every row of one instance, in one statement. Returns the conversation threads it had (their saved memory is
    separate)."""
    steps = [
        f"DELETE FROM {t('question')} WHERE run_id IN (SELECT id FROM {t('run')} WHERE period=%(k)s)",
        f"DELETE FROM {t('event')} WHERE session_id IN (SELECT id FROM {t('session')} WHERE period=%(k)s)",
        f"DELETE FROM {t('run')} WHERE period=%(k)s",
        *[f"DELETE FROM {t(n)} WHERE report_id=%(rid)s" for n in REPORT_TABLES],
        f"DELETE FROM {t('report')} WHERE id=%(rid)s",
        f"DELETE FROM {t('source_file')} WHERE workspace_id=%(k)s",
        f"DELETE FROM {t('workspace')} WHERE id=%(k)s",
    ]
    sql = ("WITH " + ", ".join(f"d{i} AS ({s})" for i, s in enumerate(steps))
           + f", gone AS (DELETE FROM {t('session')} WHERE period=%(k)s RETURNING thread_id)"
           + " SELECT coalesce(array_agg(thread_id), ARRAY[]::text[]) AS threads FROM gone")
    return c.execute(sql, {"k": k, "rid": f"fund-i-{k}"}).fetchone()["threads"]


def _move(c, t, src: str, dst: str) -> None:
    """Re-key an instance from one key to another, in one statement. The destination must hold nothing."""
    _purge(c, t, dst)
    steps = [
        *[f"UPDATE {t(n)} SET report_id=%(rd)s WHERE report_id=%(rs)s" for n in REPORT_TABLES],
        f"UPDATE {t('report')} SET id=%(rd)s WHERE id=%(rs)s",
        f"UPDATE {t('source_file')} SET workspace_id=%(kd)s WHERE workspace_id=%(ks)s",
        f"UPDATE {t('workspace')} SET id=%(kd)s WHERE id=%(ks)s",
        f"UPDATE {t('run')} SET period=%(kd)s WHERE period=%(ks)s",
        f"UPDATE {t('session')} SET period=%(kd)s WHERE period=%(ks)s",
    ]
    c.execute("WITH " + ", ".join(f"u{i} AS ({s})" for i, s in enumerate(steps)) + " SELECT 1",
              {"ks": src, "kd": dst, "rs": f"fund-i-{src}", "rd": f"fund-i-{dst}"})


def listing(store: Store, code: str) -> dict:
    """The open instance and the parked ones, and whether another can be started."""
    with store.conn() as c:
        t = store._t
        held = _holds_many(c, t, [key(code, s) for s in (0, *SLOTS)])
        items = [{"slot": 0, "current": True, **held[key(code)]}]
        for s in SLOTS:
            if not held[key(code, s)]["empty"]:
                items.append({"slot": s, "current": False, **held[key(code, s)]})
    used = len(items)
    reason = None
    if items[0]["empty"]:
        reason = "This instance is blank already, so there is nothing to keep."
    elif used >= CAP:
        reason = f"A quarter can have {CAP} instances. Delete one to make room."
    return {"cap": CAP, "used": used, "items": items, "can_create": reason is None, "reason": reason}


def new_instance(store: Store, code: str) -> dict:
    """Park the open instance and open the quarter blank."""
    with store.conn() as c:
        t = store._t
        held = _holds_many(c, t, [key(code, s) for s in (0, *SLOTS)])
        if held[key(code)]["empty"]:
            raise InstanceError("This instance is blank already, so there is nothing to keep.")
        free = next((s for s in SLOTS if held[key(code, s)]["empty"]), None)
        if free is None:
            raise InstanceError(f"A quarter can have {CAP} instances. Delete one to make room.")
        _move(c, t, key(code), key(code, free))
    return {"parked_in": free}


def switch(store: Store, code: str, slot: int) -> dict:
    """Open a parked instance; the one that was open is parked in its place (or dropped, if it was blank)."""
    if slot not in SLOTS:
        raise InstanceError("There is no such instance.")
    with store.conn() as c:
        t = store._t
        held = _holds_many(c, t, [key(code), key(code, slot)])
        if held[key(code, slot)]["empty"]:
            raise InstanceError("That instance is no longer there.")
        live = key(code)
        if held[live]["empty"]:
            _move(c, t, key(code, slot), live)            # (this also clears what the blank one left behind)
        else:                                   # swap them, through a place that is free for the moment
            tmp = key(code, 99)
            _move(c, t, live, tmp)
            _move(c, t, key(code, slot), live)
            _move(c, t, tmp, key(code, slot))
    return {"opened": slot}


def delete(store: Store, code: str, slot: int) -> dict:
    """Delete a parked instance for good (never the open one), with the agent's memory of its conversations."""
    if slot not in SLOTS:
        raise InstanceError("Only an instance that is not open can be deleted.")
    with store.conn() as c:
        t = store._t
        h = _holds(c, t, key(code, slot))
        if h["empty"] and not h["conversations"]:
            raise InstanceError("That instance is no longer there.")
        threads = _purge(c, t, key(code, slot))
        if threads and c.execute("SELECT to_regclass('checkpoints') AS r").fetchone()["r"]:
            for tbl in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                c.execute(f"DELETE FROM {tbl} WHERE thread_id = ANY(%s)", (threads,))
    return {"deleted": slot, "files": h["files"], "versions": h["versions"]}
