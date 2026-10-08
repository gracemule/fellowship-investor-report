"""Mirror the user's folder, and know what changed.

The browser walks the folder it was pointed at (File System Access API), sends a manifest
-- path, size, modified time, SHA-256 -- and the server answers with only the files it does
not already hold. After the files land, `commit` records the new state and returns what
changed, which is the trigger for updating the report.

Hashes are what decide "changed": a file re-saved with a new modified time but identical
bytes is not a change, and a file replaced in place with a new quarter's numbers is.

Each quarter has a workspace of its own (its id is the period code, "2026Q3"), so a quarter's files can never
be mixed with, or deleted by, another quarter's folder. The exception is the brand kit (the `durable` slots:
logos and fonts): it is stored once under SHARED, applies to every quarter, and a folder that happens not to
contain it never removes it.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from psycopg.types.json import Jsonb

from ..agent.store import Store
from ..extract.images import IMAGE_EXT
from .slots import SLOTS, Slot, is_durable, slot_files, slot_for_path  # noqa: F401

MAX_FILE_BYTES = 60 * 1024 * 1024
WORKSPACE = "default"
SHARED = "shared"             # files that stand across quarters

# Files the user attaches in the composer live under this prefix. They are not part of the folder
# the browser mirrors, so a folder sync must never mark them removed.
UPLOADS = "Uploads/"
ATTACH_EXT = {".pdf", ".xlsx", ".xlsm", ".xls", ".docx", ".csv", ".txt", ".md", ".json"} | IMAGE_EXT


class SyncError(ValueError):
    pass


def safe_path(path: str) -> str:
    """A relative, normalised path. Rejects anything that could escape the workspace."""
    p = path.replace("\\", "/").strip().lstrip("/")
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if not parts or any(x == ".." for x in parts) or ":" in parts[0]:
        raise SyncError(f"unsafe path {path!r}")
    return "/".join(parts)


@dataclass
class Changes:
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.added or self.modified or self.removed)

    @property
    def paths(self) -> list[str]:
        return self.added + self.modified + self.removed

    def as_dict(self) -> dict:
        return {"added": self.added, "modified": self.modified, "removed": self.removed}


def plan(store: Store, manifest: list[dict], workspace: str = WORKSPACE) -> list[str]:
    """Paths whose bytes the server needs: new, or present with a different hash."""
    have = {r["path"]: r["sha256"] for r in _present_all(store, workspace)}
    need = []
    for f in manifest:
        path = safe_path(f["path"])
        if have.get(path) != f["sha256"]:
            need.append(path)
    return need


def _present(store: Store, workspace: str = WORKSPACE) -> list[dict]:
    """The files held for one workspace only."""
    with store.conn() as c:
        return list(c.execute(
            f"SELECT path, sha256, size, mtime, status FROM {store._t('source_file')} "
            f"WHERE workspace_id=%s AND status='present' ORDER BY path", (workspace,)))


def _present_all(store: Store, workspace: str = WORKSPACE) -> list[dict]:
    """What a quarter can use: its own files plus the brand kit that stands for every quarter. A file the
    quarter holds itself wins over a shared one with the same path."""
    with store.conn() as c:
        rows = list(c.execute(
            f"SELECT workspace_id, path, sha256, size, mtime, status FROM {store._t('source_file')} "
            f"WHERE workspace_id = ANY(%s) AND status='present' ORDER BY path", ([workspace, SHARED],)))
    best: dict[str, dict] = {}
    for r in rows:
        if r["path"] not in best or r["workspace_id"] == workspace:
            best[r["path"]] = r
    return [best[k] for k in sorted(best)]


def put_file(store: Store, path: str, data: bytes, sha256: str, mtime: float | None = None,
             workspace: str = WORKSPACE) -> bool:
    """Store one uploaded file. Returns True if it is new or changed. Brand-kit files go to the shared store."""
    path = safe_path(path)
    if len(data) > MAX_FILE_BYTES:
        raise SyncError(f"{path} is {len(data) / 1e6:.0f} MB; the limit is {MAX_FILE_BYTES // 1_000_000} MB")
    actual = hashlib.sha256(data).hexdigest()
    if actual != sha256:
        raise SyncError(f"{path}: upload does not match its declared hash (corrupted in transit?)")
    workspace = SHARED if is_durable(path) else workspace
    with store.conn() as c:
        row = c.execute(f"SELECT sha256, status FROM {store._t('source_file')} "
                        f"WHERE workspace_id=%s AND path=%s", (workspace, path)).fetchone()
        if row and row["sha256"] == sha256 and row["status"] == "present":
            return False
        c.execute(
            f"""INSERT INTO {store._t('source_file')} (workspace_id, path, sha256, size, mtime, content, status)
                VALUES (%s,%s,%s,%s,%s,%s,'present')
                ON CONFLICT (workspace_id, path) DO UPDATE SET
                  sha256=EXCLUDED.sha256, size=EXCLUDED.size, mtime=EXCLUDED.mtime,
                  content=EXCLUDED.content, status='present', updated_at=now()""",
            (workspace, path, sha256, len(data), mtime, data))
    return True


def commit(store: Store, manifest: list[dict], workspace: str = WORKSPACE, folder: str | None = None,
           skipped: list[str] | None = None) -> Changes:
    """Record the folder's new state. Quarter files absent from the manifest are marked removed; the brand kit never
    is (it stands until the user changes it). Returns what changed since the previous commit (not since the last
    upload). `folder` is the name of the user's folder, kept so the page can say where the files came from; `skipped` are
    files it holds that were not sent (too large). A summary of the sync is kept for the page ("what was synced"), rewritten
    only when something changed, so the 15-second look at an unchanged folder does not wipe it."""
    wanted_all = {safe_path(f["path"]): f["sha256"] for f in manifest}
    shared = {p: h for p, h in wanted_all.items() if is_durable(p)}
    wanted = {p: h for p, h in wanted_all.items() if p not in shared}
    ch = Changes()
    t = store._t
    with store.conn() as c:
        def row_of(ws: str) -> dict:
            return c.execute(f"SELECT synced_state, settings FROM {t('workspace')} WHERE id=%s", (ws,)).fetchone() or {}

        def state_of(ws: str) -> dict[str, str]:
            return (row_of(ws).get("synced_state") or {})

        mine = row_of(workspace)
        before, before_shared = (mine.get("synced_state") or {}), state_of(SHARED)
        have = {r["path"]: r["sha256"] for r in c.execute(
            f"SELECT path, sha256 FROM {t('source_file')} WHERE workspace_id=%s AND status='present'", (workspace,))}
        have_shared = {r["path"]: r["sha256"] for r in c.execute(
            f"SELECT path, sha256 FROM {t('source_file')} WHERE workspace_id=%s AND status='present'", (SHARED,))}
        missing = [p for p, h in wanted.items() if have.get(p) != h] + [p for p, h in shared.items() if have_shared.get(p) != h]
        if missing:
            raise SyncError(f"{len(missing)} file(s) have not been uploaded yet, e.g. {missing[0]}")
        gone = [p for p in have if p not in wanted and not p.startswith(UPLOADS)]
        if gone:
            c.execute(f"UPDATE {t('source_file')} SET status='removed', content=NULL, updated_at=now() "
                      f"WHERE workspace_id=%s AND path = ANY(%s)", (workspace, gone))
        for p, h in wanted.items():
            if p not in before:
                ch.added.append(p)
            elif before[p] != h:
                ch.modified.append(p)
        for p, h in shared.items():
            if p not in before_shared:
                ch.added.append(p)
            elif before_shared[p] != h:
                ch.modified.append(p)
        ch.removed = [p for p in before if p not in wanted]
        patch: dict = {"folder": folder} if folder else {}
        if ch.any or not (mine.get("settings") or {}).get("last_sync"):
            patch["last_sync"] = {"at": datetime.now(timezone.utc).isoformat(), "folder": folder, "files": len(wanted_all), "brand": len(shared),
                                  "added": len(ch.added), "modified": len(ch.modified), "removed": len(ch.removed),
                                  "skipped": [str(x)[:200] for x in (skipped or [])][:20]}
        c.execute(
            f"""INSERT INTO {t('workspace')} AS w (id, synced_state, last_sync_at, settings) VALUES (%s,%s,now(),%s)
                ON CONFLICT (id) DO UPDATE SET synced_state=EXCLUDED.synced_state, last_sync_at=now(),
                  settings = w.settings || EXCLUDED.settings""",
            (workspace, Jsonb(wanted), Jsonb(patch)))
        if shared:
            c.execute(
                f"""INSERT INTO {t('workspace')} (id, synced_state, last_sync_at) VALUES (%s,%s,now())
                    ON CONFLICT (id) DO UPDATE SET synced_state=EXCLUDED.synced_state, last_sync_at=now()""",
                (SHARED, Jsonb({**before_shared, **shared})))
    return ch


def clear_quarter(store: Store, workspace: str, keep_attachments: bool = True) -> int:
    """Delete the files synced for one quarter (the wrong folder was chosen). The brand kit, which belongs to every
    quarter, and files attached in the conversation are left alone. Returns how many files were removed."""
    with store.conn() as c:
        rows = list(c.execute(
            f"UPDATE {store._t('source_file')} SET status='removed', content=NULL, updated_at=now() "
            f"WHERE workspace_id=%s AND status='present' AND NOT (%s AND path LIKE %s) RETURNING path",
            (workspace, keep_attachments, UPLOADS + "%")))
        c.execute(f"UPDATE {store._t('workspace')} SET synced_state='{{}}'::jsonb, last_sync_at=NULL, "
                  f"settings = settings - 'folder' - 'last_sync' WHERE id=%s", (workspace,))
    return len(rows)


def move_quarter(store: Store, source: str, target: str) -> int:
    """Move the files synced for one quarter to another (they were synced under the wrong quarter). Files already
    held under the same path by the target are replaced. Returns how many files moved."""
    if source == target:
        return 0
    t = store._t
    with store.conn() as c:
        paths = [r["path"] for r in c.execute(
            f"SELECT path FROM {t('source_file')} WHERE workspace_id=%s AND status='present' AND path NOT LIKE %s",
            (source, UPLOADS + "%"))]
        if not paths:
            return 0
        c.execute(f"DELETE FROM {t('source_file')} WHERE workspace_id=%s AND path = ANY(%s)", (target, paths))
        c.execute(f"UPDATE {t('source_file')} SET workspace_id=%s, updated_at=now() WHERE workspace_id=%s AND path = ANY(%s) "
                  f"AND status='present'", (target, source, paths))
        src = c.execute(f"SELECT synced_state, settings FROM {t('workspace')} WHERE id=%s", (source,)).fetchone() or {}
        dst = c.execute(f"SELECT synced_state, settings FROM {t('workspace')} WHERE id=%s", (target,)).fetchone() or {}
        moved = {p: h for p, h in ((src.get("synced_state") or {}).items()) if p in set(paths)}
        keep = {p: h for p, h in ((src.get("synced_state") or {}).items()) if p not in set(paths)}
        folder = (src.get("settings") or {}).get("folder")
        c.execute(f"""INSERT INTO {t('workspace')} AS w (id, synced_state, last_sync_at, settings) VALUES (%s,%s,now(),%s)
                      ON CONFLICT (id) DO UPDATE SET synced_state=EXCLUDED.synced_state, last_sync_at=now(),
                        settings = w.settings || EXCLUDED.settings""",
                  (target, Jsonb({**(dst.get("synced_state") or {}), **moved}), Jsonb({"folder": folder} if folder else {})))
        c.execute(f"UPDATE {t('workspace')} SET synced_state=%s, settings = settings - 'folder' WHERE id=%s",
                  (Jsonb(keep), source))
    return len(paths)


def materialize(store: Store, dest: Path, workspace: str = WORKSPACE, extra: dict[str, bytes] | None = None) -> int:
    """Write the quarter's files (and the brand kit) to `dest`, keeping the user's folder structure so every
    extractor that finds files by folder and pattern works unchanged. Only files whose content changed are
    rewritten, and files no longer present are deleted. `extra` adds files that are not stored as source files
    (the previous quarter's report, built here). Returns the number of files written."""
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    wrote = 0
    meta = {r["path"]: r for r in _present_all(store, workspace)}
    extra = extra or {}
    keep = set()
    stale = []
    for path, r in meta.items():
        target = (dest / path).resolve()
        if dest not in target.parents:
            raise SyncError(f"path escapes the workspace: {path}")
        keep.add(target)
        if not (target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == r["sha256"]):
            stale.append(path)
    for path, data in extra.items():
        target = (dest / safe_path(path)).resolve()
        keep.add(target)
        if not (target.exists() and target.read_bytes() == data):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            wrote += 1
    # Only the files that differ are fetched, one at a time: no need to move the whole folder
    # across the network (or hold it in memory) when most of it is already on disk.
    for path in stale:
        with store.conn() as c:
            row = c.execute(f"SELECT content FROM {store._t('source_file')} WHERE workspace_id=%s AND path=%s "
                            f"AND status='present'", (meta[path]["workspace_id"], path)).fetchone()
        if row is None or row["content"] is None:
            continue
        target = (dest / path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bytes(row["content"]))
        wrote += 1
    for f in [p for p in dest.rglob("*") if p.is_file()]:
        if f.resolve() not in keep:
            f.unlink()
    for d in sorted([p for p in dest.rglob("*") if p.is_dir()], key=lambda p: -len(p.parts)):
        if not any(d.iterdir()):
            shutil.rmtree(d, ignore_errors=True)
    return wrote


# ---- readiness --------------------------------------------------------------------------


@dataclass
class SlotStatus:
    slot: Slot
    files: list[str]
    modified: list[str]
    note: str = ""                          # where it comes from when it is not a file in the folder

    @property
    def state(self) -> str:
        if len(self.files) >= self.slot.minimum or (self.note and not self.files):
            return "ready"
        return "missing" if self.slot.required else "absent"

    def as_dict(self) -> dict:
        return {"id": self.slot.id, "label": self.slot.label, "folder": self.slot.folder,
                "required": self.slot.required, "state": self.state, "count": len(self.files),
                "files": self.files, "modified": self.modified, "unlocks": list(self.slot.unlocks),
                "hint": self.slot.hint, "durable": self.slot.durable, "system": self.slot.system, "note": self.note}


def coverage_of(paths: list[str], changed: list[str] | None = None, prior: dict | None = None) -> list[SlotStatus]:
    """Which sources are in. `prior` ({"label": "Q2 2026", "version": 8}) is the previous quarter's report as built
    here: it stands in for the baseline PDF, so a quarter that follows one built here never asks for it."""
    changed = changed or []
    out = []
    for s in SLOTS:
        st = SlotStatus(s, slot_files(s, paths), slot_files(s, changed))
        if s.id == "prior_report" and not st.files and prior:
            st.note = f"The {prior['label']} report built here (version {prior['version']})"
        out.append(st)
    return out


def coverage(store: Store, changed: list[str] | None = None, workspace: str = WORKSPACE,
             prior: dict | None = None) -> list[SlotStatus]:
    return coverage_of([r["path"] for r in _present_all(store, workspace)], changed, prior)


def required_missing(cov: list[SlotStatus]) -> list[SlotStatus]:
    """Everything a report cannot be built without, the brand kit included."""
    return [c for c in cov if c.state == "missing"]


def user_missing(cov: list[SlotStatus]) -> list[SlotStatus]:
    """What the user still has to provide for this quarter (the system's brand kit is not theirs to provide each time)."""
    return [c for c in cov if c.state == "missing" and not c.slot.system]


def system_missing(cov: list[SlotStatus]) -> list[SlotStatus]:
    return [c for c in cov if c.state == "missing" and c.slot.system]


def unplaced(store: Store, workspace: str = WORKSPACE) -> list[str]:
    """Files in the folder that no slot recognises: shown so nothing is silently ignored."""
    return [r["path"] for r in _present_all(store, workspace) if slot_for_path(r["path"]) is None
            and not r["path"].casefold().startswith(("branding/", UPLOADS.casefold()))]


def affected_sections(paths: list[str]) -> list[str]:
    out: set[str] = set()
    for p in paths:
        s = slot_for_path(p)
        if s:
            out.update(s.unlocks)
    return sorted(out, key=lambda k: [int(x) for x in k.split(".")])


def affected_slots(paths: list[str]) -> list[Slot]:
    seen: dict[str, Slot] = {}
    for p in paths:
        s = slot_for_path(p)
        if s:
            seen[s.id] = s
    return list(seen.values())


# ---- attachments ------------------------------------------------------------------------


def clean_attachment_name(name: str) -> str:
    base = re.sub(r"[^\w .()+&,'-]", "_", os.path.basename(name.replace("\\", "/")).strip()) or "attachment"
    return base[:120]


def put_attachment(store: Store, name: str, data: bytes, workspace: str = WORKSPACE) -> dict:
    """Store a file the user attached to a message. A different file with the same name is kept
    alongside ("name (2).pdf") rather than silently replacing the first."""
    name = clean_attachment_name(name)
    stem, ext = os.path.splitext(name)
    if ext.lower() not in ATTACH_EXT:
        raise SyncError(f"{name}: this kind of file cannot be attached (PDF, Excel, Word, CSV, text and images can)")
    sha = hashlib.sha256(data).hexdigest()
    with store.conn() as c:
        taken = {r["path"]: r["sha256"] for r in c.execute(
            f"SELECT path, sha256 FROM {store._t('source_file')} WHERE workspace_id=%s AND status='present' "
            f"AND path LIKE %s", (workspace, UPLOADS + "%"))}
    path, n = UPLOADS + name, 1
    while path in taken and taken[path] != sha:
        n += 1
        path = f"{UPLOADS}{stem} ({n}){ext}"
    put_file(store, path, data, sha, None, workspace)
    return {"path": path, "name": path[len(UPLOADS):], "size": len(data),
            "kind": "image" if ext.lower() in IMAGE_EXT else "document"}


def remove_attachment(store: Store, path: str, workspace: str = WORKSPACE) -> bool:
    path = safe_path(path)
    if not path.startswith(UPLOADS):
        raise SyncError("only attached files can be removed this way")
    with store.conn() as c:
        r = c.execute(f"UPDATE {store._t('source_file')} SET status='removed', content=NULL, updated_at=now() "
                      f"WHERE workspace_id=%s AND path=%s AND status='present' RETURNING path", (workspace, path)).fetchone()
    return bool(r)


def attachments(store: Store, workspace: str = WORKSPACE) -> list[dict]:
    return [{"path": r["path"], "name": r["path"][len(UPLOADS):], "size": r["size"]}
            for r in _present(store, workspace) if r["path"].startswith(UPLOADS)]
