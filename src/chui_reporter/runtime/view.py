"""One authoritative description of 'where things stand', for the interface.

The wording and the single recommended next action are decided here, once, so the browser
stays a thin renderer and cannot drift from the server's idea of the state. The interface is
built around one question -- what should this person do next? -- and the answer is computed
rather than left to the front end to infer.
"""

from __future__ import annotations

from ..agent.llm import describe
from ..workspace import sync as ws_sync
from ..workspace.slots import is_durable
from . import state


def _labels(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _latest_step(store, run_id: str) -> str | None:
    for e in reversed(state.recent_events(store, 40)):
        if e["run_id"] == run_id and e["kind"] == "step":
            return e["label"]
    return None


def build(rt) -> dict:
    from .. import period as pr

    store = rt.store
    period = rt.period()
    rows = state.snapshot_rows(store, period=period.code)
    ws = rows["workspace"]
    fws = rows.get("files_ws") or {}                     # the quarter's own workspace row: when and from where it was synced
    paths = rows["paths"]
    pv = rows.get("prior_version")
    prior = {"label": period.prev.label, "version": pv["version"]} if pv else None
    cov = ws_sync.coverage_of(paths, prior=prior)
    # What this quarter itself holds: not the brand kit (it stands for every quarter), not files attached in a message.
    quarter_files = [p for p in paths if not is_durable(p) and not p.startswith(ws_sync.UPLOADS)]
    folder = (fws.get("settings") or {}).get("folder")
    synced_at = fws.get("last_sync_at")
    run = rows["run"]
    questions = rows["questions"]
    versions = rows["versions"]
    version = versions[0] if versions else None
    missing = ws_sync.user_missing(cov)
    brand_missing = ws_sync.system_missing(cov)
    pending = rt._pending.any
    active = bool(run and run["status"] in state.ACTIVE)
    files = quarter_files

    phase, headline, detail, action = "idle", "", "", None
    if active:
        st = run["status"]
        if st == "waiting_user":
            q = questions[0] if questions else None
            phase, headline = "waiting_user", "A question for you"
            detail = q["prompt"] if q else ""
        elif st == "waiting_data":
            q = questions[0] if questions else None
            slots = (q or {}).get("slots") or []
            phase, headline = "waiting_data", "Waiting for files"
            detail = "Add " + _labels([c.slot.label for c in cov if c.slot.id in slots]) + " to your folder." if slots else ""
        elif st == "queued":
            phase, headline = "working", "Starting"
        else:
            phase, headline = "working", "Working on the report"
            detail = _latest_step(store, run["id"]) or ""
        action = {"id": "stop", "label": "Stop"}
    elif not files:
        if synced_at:       # a folder was connected, and it holds nothing for this quarter (yet)
            phase, headline = "empty_folder", f"“{folder or 'The folder'}” is empty"
            detail = f"Put the {period.label} files in it. They are picked up on their own."
            action = {"id": "pick", "label": "Choose another folder"}
        else:
            phase, headline = "empty", f"Choose the {period.label} folder"
            detail = "The report is built from one folder on your computer."
            action = {"id": "pick", "label": "Choose folder"}
    elif missing:
        phase, headline = "needs_sources", f"{len(missing)} {'thing' if len(missing) == 1 else 'things'} still needed"
        detail = "Add " + _labels([m.slot.label for m in missing]) + "."
        action = {"id": "pick", "label": "Add files"}
    elif brand_missing:
        phase, headline = "needs_brand", "The brand kit is not installed yet"
        detail = ("Choose a folder that contains your Branding folder, once. It is kept for every quarter, so this is never asked "
                  "for again.")
        action = {"id": "pick", "label": "Choose folder"}
    elif run and run["status"] in ("failed", "incomplete", "stopped") and (
            not version or run["created_at"] > version["created_at"]):
        phase = "attention"
        headline = {"failed": "The last update stopped", "incomplete": "The last update did not finish",
                    "stopped": "You stopped the last update"}[run["status"]]
        detail = run.get("error") or ""
        action = {"id": "resume", "label": "Continue"}
    elif version is None:
        phase, headline = "ready", f"Ready to build the {period.label} report"
        detail = "Everything required is in your folder."
        action = {"id": "build", "label": "Build the report"}
    elif pending:
        phase, headline = "stale", "Your folder has changed"
        detail = "The report does not yet reflect the new files."
        action = {"id": "update", "label": "Update the report"}
    else:
        phase, headline = "current", "The report is up to date"

    return {
        "workspace": {"name": ws["name"], "last_sync_at": synced_at, "folder": folder,
                      "last_sync": (fws.get("settings") or {}).get("last_sync"),
                      "auto": bool((ws.get("settings") or {}).get("auto", True)), "files": len(files)},
        "period": {"code": period.code, "label": period.label, "prev": period.prev.label, "prev_code": period.prev.code,
                   "next": period.next.label, "next_code": period.next.code, "end": period.end_label,
                   "has_report": version is not None, "prior": prior},
        "status": {"phase": phase, "headline": headline, "detail": detail, "action": action},
        "run": ({k: run.get(k) for k in ("id", "kind", "status", "error", "created_at", "usage")} if run else None),
        "questions": questions,
        "coverage": [c.as_dict() for c in cov],
        "unplaced": [p for p in paths if ws_sync.slot_for_path(p) is None
                     and not p.casefold().startswith(("branding/", ws_sync.UPLOADS.casefold()))],
        "attachments": [p[len(ws_sync.UPLOADS):] for p in paths if p.startswith(ws_sync.UPLOADS)],
        "pending": {"count": len(rt._pending.paths), "sections": ws_sync.affected_sections(rt._pending.paths)}
        if pending else None,
        "version": version,
        "versions": versions,
        "provider": _provider_label(rt),
        "session": {"id": rt.session()["id"]},
        "services": _services(rows),
        "last_event_id": rows["last_event"],
    }


def _provider_label(rt) -> str:
    try:
        return describe(rt.provider_override)
    except Exception:       # noqa: BLE001
        return ""


def _services(rows: dict) -> dict:
    from ..services import status

    return status.build(rows.get("service_state") or [], rows.get("service_usage") or [])
