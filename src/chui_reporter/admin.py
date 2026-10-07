"""Operator commands. Not part of the web app: run from a shell with the database URL in the environment.

    python -m chui_reporter.admin reset-quarter 2026Q2             # shows what it would delete
    python -m chui_reporter.admin reset-quarter 2026Q2 --apply     # deletes it
    python -m chui_reporter.admin install-brand "/path/to/folder"  # installs the brand kit (logos, fonts), once, for every quarter

`reset-quarter` returns one quarter to blank, as if nothing had ever been done for it: the files synced for it, its report (sections,
tables, charts, figures, versions, review notes), its runs, questions, conversations (sessions and the agent's saved memory) and
activity feed. Other quarters are not touched. The brand kit (shared by every quarter) is kept unless `--brand` is given; the
cache of web pages the researchers read is emptied unless `--keep-web-cache`. It refuses while a run is active, and it is one
transaction: a failure changes nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from psycopg.types.json import Jsonb

from . import period as pr
from .agent.store import Store
from .runtime import state
from .workspace import instances
from .workspace import sync as ws_sync


class Busy(RuntimeError):
    pass


class _DryRun(Exception):
    pass


REPORT_TABLES = instances.REPORT_TABLES
CLI_REPORT = "chui-fund-i"          # the report the command-line agent (before the web app) wrote to; the web app never reads it


def set_period(store: Store, period: str) -> str:
    """Point the workspace at a quarter (what the page opens on). Refused while a run is active."""
    code = pr.Period.parse(period).code
    with store.conn() as c:
        busy = c.execute(f"SELECT count(*) AS n FROM {store._t('run')} WHERE status = ANY(%s)", (list(state.ACTIVE),)).fetchone()["n"]
        if busy:
            raise Busy(f"{busy} run(s) are active or waiting for an answer; stop them first")
    state.update_workspace(store, period=code)
    return code


def install_brand(store: Store, folder: str | Path) -> dict:
    """Install the brand kit (the `Branding` folder: logos, fonts) once, for every quarter. It is the system's, not a quarter's.

    `folder` is the folder that contains `Branding`, or `Branding` itself. Files are stored under the shared workspace, and what is
    held is remembered so that a later folder sync of the same files reads as "no change".
    """
    root = Path(folder).expanduser()
    if root.name.casefold() == "branding":
        root = root.parent
    brand = next((d for d in root.iterdir() if d.is_dir() and d.name.casefold() == "branding"), None) if root.is_dir() else None
    if brand is None:
        raise FileNotFoundError(f"no Branding folder in {root}")
    held, stored = {}, 0
    for f in sorted(brand.rglob("*")):
        if not f.is_file() or f.name.startswith((".", "~$")):
            continue
        rel = f.relative_to(root).as_posix()
        data = f.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        stored += bool(ws_sync.put_file(store, rel, data, sha, f.stat().st_mtime * 1000))
        held[rel] = sha
    if not held:
        raise FileNotFoundError(f"{brand} holds no files")
    with store.conn() as c:
        row = c.execute(f"SELECT synced_state FROM {store._t('workspace')} WHERE id=%s", (ws_sync.SHARED,)).fetchone()
        c.execute(f"INSERT INTO {store._t('workspace')} (id, synced_state, last_sync_at) VALUES (%s,%s,now()) "
                  f"ON CONFLICT (id) DO UPDATE SET synced_state=EXCLUDED.synced_state, last_sync_at=now()",
                  (ws_sync.SHARED, Jsonb({**((row or {}).get("synced_state") or {}), **held})))
    cov = {c.slot.id: c.state for c in ws_sync.coverage(store, workspace=ws_sync.SHARED)}
    return {"files": len(held), "stored": stored, "logos": cov.get("brand_logos"), "fonts": cov.get("brand_fonts")}


def reset_quarter(store: Store, period: str, *, brand: bool = False, web_cache: bool = True, memory: bool = True,
                  cli_report: bool = False, apply: bool = False) -> dict:
    code = pr.Period.parse(period).code
    rid = f"fund-i-{code}"
    t = store._t
    counts: dict[str, int] = {}
    try:
        with store.conn() as c:
            busy = c.execute(f"SELECT count(*) AS n FROM {t('run')} WHERE status = ANY(%s)", (list(state.ACTIVE),)).fetchone()["n"]
            if busy:
                raise Busy(f"{busy} run(s) are active or waiting for an answer; stop them first")
            sessions = list(c.execute(f"SELECT id, thread_id, period FROM {t('session')} ORDER BY created_at"))
            mine = [s for s in sessions if s["period"] == code]
            first_period = sessions[0]["period"] if sessions else None
            sids, threads = [s["id"] for s in mine], [s["thread_id"] for s in mine]

            def run(name: str, sql: str, args=()) -> None:
                counts[name] = c.execute(sql, args).rowcount

            run("questions", f"DELETE FROM {t('question')} WHERE run_id IN (SELECT id FROM {t('run')} WHERE period=%s)", (code,))
            run("events", f"DELETE FROM {t('event')} WHERE session_id = ANY(%s) OR (session_id IS NULL AND %s)",
                (sids, first_period == code))
            run("runs", f"DELETE FROM {t('run')} WHERE period=%s", (code,))
            run("sessions", f"DELETE FROM {t('session')} WHERE period=%s", (code,))
            for name in REPORT_TABLES:
                run(name, f"DELETE FROM {t(name)} WHERE report_id=%s", (rid,))
            run("report", f"DELETE FROM {t('report')} WHERE id=%s", (rid,))
            if cli_report:
                for name in REPORT_TABLES:
                    counts[f"cli_report_{name}"] = c.execute(f"DELETE FROM {t(name)} WHERE report_id=%s", (CLI_REPORT,)).rowcount
                counts["cli_report"] = c.execute(f"DELETE FROM {t('report')} WHERE id=%s", (CLI_REPORT,)).rowcount
            run("files", f"DELETE FROM {t('source_file')} WHERE workspace_id = ANY(%s)", ([code, "default"] + (["shared"] if brand else []),))
            run("workspaces", f"DELETE FROM {t('workspace')} WHERE id = ANY(%s)", ([code] + (["shared"] if brand else []),))
            if web_cache:
                run("web_pages", f"DELETE FROM {t('web_snapshot')}")
            if memory and c.execute("SELECT to_regclass('checkpoints') AS r").fetchone()["r"]:
                other = {s["thread_id"] for s in sessions if s["period"] != code}
                orphans = [r["thread_id"] for r in c.execute("SELECT DISTINCT thread_id FROM checkpoints")
                           if r["thread_id"] not in other and r["thread_id"] not in threads]
                gone = list(dict.fromkeys(threads + orphans))
                counts["conversations_memory"] = len(gone)
                for tbl in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                    c.execute(f"DELETE FROM {tbl} WHERE thread_id = ANY(%s)", (gone,))
            if not apply:
                raise _DryRun
    except _DryRun:
        return {"period": code, "applied": False, "counts": counts}
    return {"period": code, "applied": True, "counts": counts}


def main(argv: list[str] | None = None) -> int:
    from dotenv import find_dotenv, load_dotenv

    ap = argparse.ArgumentParser(prog="chui_reporter.admin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rq = sub.add_parser("reset-quarter", help="return one quarter to blank")
    rq.add_argument("period", help="for example 2026Q2")
    rq.add_argument("--apply", action="store_true", help="do it (otherwise only report)")
    rq.add_argument("--brand", action="store_true", help="also remove the shared brand kit")
    rq.add_argument("--keep-web-cache", action="store_true", help="keep the pages the researchers read")
    rq.add_argument("--keep-memory", action="store_true", help="keep the agent's saved memory of the conversations")
    rq.add_argument("--cli-report", action="store_true", help="also delete the report the old command-line agent wrote")
    sp = sub.add_parser("set-period", help="open the workspace on a quarter")
    sp.add_argument("period", help="for example 2026Q2")
    ib = sub.add_parser("install-brand", help="install the brand kit (logos and fonts), once, for every quarter")
    ib.add_argument("folder", help="the folder that contains Branding")
    args = ap.parse_args(argv)
    load_dotenv(find_dotenv(usecwd=True), override=False)
    if args.cmd == "install-brand":
        try:
            rep = install_brand(Store(), args.folder)
        except FileNotFoundError as exc:
            print(f"not done: {exc}", file=sys.stderr)
            return 2
        print(f"brand kit installed: {rep['files']} files ({rep['stored']} newly stored); logos {rep['logos']}, fonts {rep['fonts']}")
        return 0
    if args.cmd == "set-period":
        try:
            print("workspace now opens on", set_period(Store(), args.period))
            return 0
        except Busy as exc:
            print(f"not done: {exc}", file=sys.stderr)
            return 2
    try:
        rep = reset_quarter(Store(), args.period, brand=args.brand, web_cache=not args.keep_web_cache,
                            memory=not args.keep_memory, cli_report=args.cli_report, apply=args.apply)
    except Busy as exc:
        print(f"not done: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"nothing was changed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    verb = "deleted" if rep["applied"] else "would delete"
    print(f"{rep['period']}: {verb}")
    for k, v in rep["counts"].items():
        print(f"  {k:<22}{v:>7}")
    if rep["applied"]:
        if not args.keep_memory:       # the deleted conversations' rows leave bloat behind; give the space back
            import psycopg
            from psycopg.rows import dict_row

            from .runtime import retention
            with psycopg.connect(Store().url, autocommit=True, prepare_threshold=None, row_factory=dict_row) as cx:
                try:
                    print("  saved memory rebuilt:", retention.compact(cx, apply=True)["rows_kept"])
                except Exception as exc:  # noqa: BLE001
                    print(f"  (could not shrink the saved-memory tables: {exc})")
    else:
        print("nothing was changed. Add --apply to do it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
