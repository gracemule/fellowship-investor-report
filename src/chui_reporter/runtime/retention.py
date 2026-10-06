"""Keeping the agent's saved working memory small.

The agent saves a checkpoint after every step, and each checkpoint carries a full copy of the conversation so far, so a long
conversation costs far more than it holds (a few hundred megabytes for conversations whose final state is under a megabyte).
Resuming only ever needs the newest checkpoint (or the last couple, if a step failed halfway). The older ones are a step-by-step
history that nothing here reads.

What this module does, in order of how often:

* `prune_thread`: after every ~20 steps of a run, and when it ends, delete all but the newest few checkpoints of that
  conversation, with the saved writes and conversation copies only they used. Called between steps, from the thread that runs the
  agent, so it never races a step in flight. It does not shrink the files Postgres keeps (the space is reused), it stops growth.
* `enforce_budget`: a distant safety net. If the saved payload of all conversations passes a budget (150 MB by default; the
  heaviest conversation seen is under 1 MB), the saved state of the longest-idle conversations is dropped, never one that is
  running or parked on a question. Nothing is dropped on a timer.
* `compact`: a one-time clean-up for a database that has already grown: keep the newest few checkpoints of every conversation,
  empty the three tables (which really returns the space), and put the kept rows back, all in one transaction.

    python -m chui_reporter.runtime.retention            # shows what it would do
    python -m chui_reporter.runtime.retention --apply    # does it

The library's own `prune` is not implemented for Postgres, so this works on its three tables directly: `checkpoints` (one row per
step; `channel_versions` says which saved copies of each channel the step used), `checkpoint_blobs` (those copies, shared between
steps that did not change that channel) and `checkpoint_writes` (results saved for the step in progress).
"""

from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger("uvicorn.error")

KEEP = int(os.environ.get("CHUI_CHECKPOINTS_KEPT", "2"))          # newest checkpoints kept per conversation
PRUNE_EVERY = int(os.environ.get("CHUI_PRUNE_EVERY", "20"))       # steps between prunes inside a run
BUDGET_MB = float(os.environ.get("CHUI_CHECKPOINT_BUDGET_MB", "150"))
TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


def _t(name: str, schema: str | None) -> str:
    return f"{schema}.{name}" if schema else name


def _one(cx, sql: str, args=()):
    row = cx.execute(sql, args).fetchone()
    return list(row.values())[0] if isinstance(row, dict) else row[0]


def prune_thread(cx, thread_id: str, keep: int | None = None, schema: str | None = None) -> dict:
    """Keep the newest `keep` checkpoints of one conversation (per namespace); delete the rest and what only they used."""
    keep = max(1, KEEP if keep is None else keep)
    cp, bl, wr = (_t(n, schema) for n in TABLES)
    with cx.transaction():
        c = cx.execute(
            f"""WITH ranked AS (SELECT checkpoint_ns, checkpoint_id,
                                       row_number() OVER (PARTITION BY checkpoint_ns ORDER BY checkpoint_id DESC) rn
                                FROM {cp} WHERE thread_id=%s)
                DELETE FROM {cp} c USING ranked r
                WHERE c.thread_id=%s AND c.checkpoint_ns=r.checkpoint_ns AND c.checkpoint_id=r.checkpoint_id AND r.rn > %s""",
            (thread_id, thread_id, keep)).rowcount
        if not c:
            return {"checkpoints": 0, "writes": 0, "blobs": 0}
        w = cx.execute(
            f"""DELETE FROM {wr} w WHERE w.thread_id=%s AND NOT EXISTS (
                  SELECT 1 FROM {cp} c WHERE c.thread_id=w.thread_id AND c.checkpoint_ns=w.checkpoint_ns
                                         AND c.checkpoint_id=w.checkpoint_id)""", (thread_id,)).rowcount
        b = cx.execute(
            f"""DELETE FROM {bl} b WHERE b.thread_id=%s AND NOT EXISTS (
                  SELECT 1 FROM {cp} c WHERE c.thread_id=b.thread_id AND c.checkpoint_ns=b.checkpoint_ns
                                         AND (c.checkpoint->'channel_versions'->>b.channel) = b.version)""", (thread_id,)).rowcount
    return {"checkpoints": c, "writes": w, "blobs": b}


def threads_over(cx, keep: int | None = None, schema: str | None = None) -> list[str]:
    """Conversations holding more than `keep` checkpoints."""
    keep = max(1, KEEP if keep is None else keep)
    rows = cx.execute(f"SELECT thread_id FROM {_t('checkpoints', schema)} GROUP BY thread_id, checkpoint_ns HAVING count(*) > %s",
                      (keep,)).fetchall()
    return sorted({(r["thread_id"] if isinstance(r, dict) else r[0]) for r in rows})


def prune_all(cx, keep: int | None = None, skip=(), schema: str | None = None) -> int:
    """Prune every conversation that needs it except `skip` (those in use). Returns checkpoints deleted."""
    n = 0
    for t in threads_over(cx, keep, schema):
        if t not in skip:
            n += prune_thread(cx, t, keep, schema)["checkpoints"]
    return n


def payloads(cx, schema: str | None = None) -> dict[str, int]:
    """Bytes of saved payload per conversation (uncompressed, so a safe overestimate of what is stored)."""
    cp, bl, wr = (_t(n, schema) for n in TABLES)
    out: dict[str, int] = {}
    for sql in (f"SELECT thread_id, sum(octet_length(checkpoint::text)) n FROM {cp} GROUP BY 1",
                f"SELECT thread_id, sum(coalesce(octet_length(blob),0)) n FROM {bl} GROUP BY 1",
                f"SELECT thread_id, sum(octet_length(blob)) n FROM {wr} GROUP BY 1"):
        for r in cx.execute(sql).fetchall():
            t, n = (r["thread_id"], r["n"]) if isinstance(r, dict) else (r[0], r[1])
            out[t] = out.get(t, 0) + int(n or 0)
    return out


def enforce_budget(cx, idle_first: list[str], protected=(), budget_mb: float | None = None, schema: str | None = None) -> list[str]:
    """If the saved payload passes the budget, drop whole conversations' saved state, longest idle first (`idle_first`, with
    conversations unknown to it counted as idle first), never one in `protected`, until it is back under 75% of the budget.
    Returns the conversations dropped. A distant safety net: nothing is dropped below the budget."""
    budget = int((BUDGET_MB if budget_mb is None else budget_mb) * 1024 * 1024)
    sizes = payloads(cx, schema)
    total = sum(sizes.values())
    if total <= budget:
        return []
    known = [t for t in idle_first if t in sizes]
    order = [t for t in sizes if t not in known] + known
    cp, bl, wr = (_t(n, schema) for n in TABLES)
    dropped = []
    for t in order:
        if total <= budget * 0.75:
            break
        if t in protected:
            continue
        with cx.transaction():
            for tbl in (wr, bl, cp):
                cx.execute(f"DELETE FROM {tbl} WHERE thread_id=%s", (t,))
        total -= sizes.get(t, 0)
        dropped.append(t)
    if dropped:
        log.warning("saved agent memory passed its budget (%d MB); dropped the state of %d idle conversation(s)",
                    budget // 1048576, len(dropped))
    return dropped


def table_sizes(cx, schema: str | None = None) -> dict[str, int]:
    return {n: int(_one(cx, "SELECT pg_total_relation_size(%s::regclass)", (_t(n, schema),))) for n in TABLES}


def compact(cx, keep: int | None = None, schema: str | None = None, apply: bool = False) -> dict:
    """Keep the newest `keep` checkpoints of every conversation and rebuild the three tables from them, which returns the space
    Postgres would otherwise keep. One transaction: if anything fails, nothing changes. `apply=False` only reports."""
    keep = max(1, KEEP if keep is None else keep)
    cp, bl, wr = (_t(n, schema) for n in TABLES)
    report: dict = {"before": table_sizes(cx, schema)}
    try:
        _compact(cx, keep, cp, bl, wr, schema, apply, report)
    except _DryRun:
        report["applied"] = False
        return report
    report["after"] = table_sizes(cx, schema)
    report["applied"] = True
    return report


def _compact(cx, keep, cp, bl, wr, schema, apply, report) -> None:
    with cx.transaction():
        report["rows_before"] = {n: int(_one(cx, f"SELECT count(*) FROM {_t(n, schema)}")) for n in TABLES}
        cx.execute(f"""CREATE TEMP TABLE _keep_cp ON COMMIT DROP AS
                       SELECT * FROM {cp} WHERE (thread_id, checkpoint_ns, checkpoint_id) IN (
                         SELECT thread_id, checkpoint_ns, checkpoint_id FROM (
                           SELECT thread_id, checkpoint_ns, checkpoint_id,
                                  row_number() OVER (PARTITION BY thread_id, checkpoint_ns ORDER BY checkpoint_id DESC) rn
                           FROM {cp}) x WHERE rn <= %s)""", (keep,))
        cx.execute(f"""CREATE TEMP TABLE _keep_wr ON COMMIT DROP AS
                       SELECT w.* FROM {wr} w JOIN _keep_cp k USING (thread_id, checkpoint_ns, checkpoint_id)""")
        cx.execute(f"""CREATE TEMP TABLE _keep_bl ON COMMIT DROP AS
                       SELECT b.* FROM {bl} b WHERE EXISTS (
                         SELECT 1 FROM _keep_cp c WHERE c.thread_id=b.thread_id AND c.checkpoint_ns=b.checkpoint_ns
                                                    AND (c.checkpoint->'channel_versions'->>b.channel) = b.version)""")
        kept = {"checkpoints": int(_one(cx, "SELECT count(*) FROM _keep_cp")),
                "checkpoint_writes": int(_one(cx, "SELECT count(*) FROM _keep_wr")),
                "checkpoint_blobs": int(_one(cx, "SELECT count(*) FROM _keep_bl"))}
        report["rows_kept"] = kept
        threads_before = int(_one(cx, f"SELECT count(DISTINCT thread_id) FROM {cp}"))
        if int(_one(cx, "SELECT count(DISTINCT thread_id) FROM _keep_cp")) != threads_before:
            raise RuntimeError("a conversation would lose all its checkpoints; nothing was changed")
        if not apply:
            raise _DryRun
        cx.execute(f"TRUNCATE {cp}, {bl}, {wr}")
        cx.execute(f"INSERT INTO {cp} SELECT * FROM _keep_cp")
        cx.execute(f"INSERT INTO {bl} SELECT * FROM _keep_bl")
        cx.execute(f"INSERT INTO {wr} SELECT * FROM _keep_wr")
        after = {"checkpoints": int(_one(cx, f"SELECT count(*) FROM {cp}")), "checkpoint_writes": int(_one(cx, f"SELECT count(*) FROM {wr}")),
                 "checkpoint_blobs": int(_one(cx, f"SELECT count(*) FROM {bl}"))}
        if after != kept:
            raise RuntimeError(f"the rebuilt tables do not match what was kept ({after} != {kept}); nothing was changed")


class _DryRun(Exception):
    pass


def main(argv: list[str] | None = None) -> int:
    import argparse

    import psycopg
    from dotenv import find_dotenv, load_dotenv
    from psycopg.rows import dict_row

    ap = argparse.ArgumentParser(prog="retention", description="Shrink the agent's saved checkpoints.")
    ap.add_argument("--apply", action="store_true", help="do it (otherwise only report)")
    ap.add_argument("--keep", type=int, default=KEEP, help="newest checkpoints to keep per conversation")
    args = ap.parse_args(argv)
    load_dotenv(find_dotenv(usecwd=True), override=False)
    from ..agent.store import Store
    from . import state

    store = Store()
    with store.conn() as c:                      # never rebuild the tables under a run that is writing to them
        busy = c.execute(f"SELECT count(*) AS n FROM {store._t('run')} WHERE status = ANY(%s)", (list(state.ACTIVE),)).fetchone()["n"]
    if busy:
        print(f"{busy} run(s) are active or waiting; try again when none are.", file=sys.stderr)
        return 2
    with psycopg.connect(store.url, autocommit=True, prepare_threshold=None, row_factory=dict_row) as cx:
        try:
            rep = compact(cx, args.keep, apply=args.apply)
        except Exception as exc:  # noqa: BLE001
            print(f"nothing was changed: {exc}", file=sys.stderr)
            return 1
        pay = payloads(cx)
    mb = lambda d: {k: f"{v / 1048576:.1f} MB" for k, v in d.items()}  # noqa: E731
    print("rows now:", rep["rows_before"], "| would keep:" if not rep["applied"] else "| kept:", rep["rows_kept"])
    print("on disk before:", mb(rep["before"]))
    if rep["applied"]:
        print("on disk after: ", mb(rep["after"]))
    else:
        print(f"dry run: nothing changed. {len(pay)} conversations, {sum(pay.values()) / 1048576:.1f} MB of saved payload (uncompressed).")
        print(f"--apply keeps the newest {args.keep} checkpoints of each and returns the rest of the space.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
