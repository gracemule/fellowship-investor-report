"""Mirror the user's folder, and know what changed.

The browser walks the folder it was pointed at (File System Access API), sends a manifest
-- path, size, modified time, SHA-256 -- and the server answers with only the files it does
not already hold. After the files land, `commit` records the new state and returns what
changed, which is the trigger for updating the report.

Hashes are what decide "changed": a file re-saved with a new modified time but identical
bytes is not a change, and a file replaced in place with a new quarter's numbers is.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from psycopg.types.json import Jsonb

from ..agent.store import Store
from .slots import SLOTS, Slot, slot_files, slot_for_path  # noqa: F401

MAX_FILE_BYTES = 60 * 1024 * 1024
WORKSPACE = "default"

# Files the user attaches in the composer live under this prefix. They are not part of the folder
# the browser mirrors, so a folder sync must never mark them removed.
UPLOADS = "Uploads/"
ATTACH_EXT = {".pdf", ".xlsx", ".xlsm", ".xls", ".docx", ".csv", ".txt", ".md", ".json",
              ".png", ".jpg", ".jpeg", ".webp", ".gif"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


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
    have = {r["path"]: r["sha256"] for r in _present(store, workspace)}
    need = []
    for f in manifest:
        path = safe_path(f["path"])
        if have.get(path) != f["sha256"]:
            need.append(path)
    return need


def _present(store: Store, workspace: str = WORKSPACE) -> list[dict]:
    with store.conn() as c:
        return list(c.execute(
            f"SELECT path, sha256, size, mtime, status FROM {store._t('source_file')} "
            f"WHERE workspace_id=%s AND status='present' ORDER BY path", (workspace,)))


def put_file(store: Store, path: str, data: bytes, sha256: str, mtime: float | None = None,
             workspace: str = WORKSPACE) -> bool:
    """Store one uploaded file. Returns True if it is new or changed."""
    path = safe_path(path)
    if len(data) > MAX_FILE_BYTES:
        raise SyncError(f"{path} is {len(data) / 1e6:.0f} MB; the limit is {MAX_FILE_BYTES // 1_000_000} MB")
    actual = hashlib.sha256(data).hexdigest()
    if actual != sha256:
        raise SyncError(f"{path}: upload does not match its declared hash (corrupted in transit?)")
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


def commit(store: Store, manifest: list[dict], workspace: str = WORKSPACE) -> Changes:
    """Record the folder's new state. Files absent from the manifest are marked removed;
    returns what changed since the previous commit (not since the last upload)."""
    wanted = {safe_path(f["path"]): f["sha256"] for f in manifest}
    ch = Changes()
    with store.conn() as c:
        prev = c.execute(f"SELECT synced_state FROM {store._t('workspace')} WHERE id=%s",
                         (workspace,)).fetchone()
        before: dict[str, str] = (prev["synced_state"] if prev else {}) or {}
        have = {r["path"]: r["sha256"] for r in c.execute(
            f"SELECT path, sha256 FROM {store._t('source_file')} WHERE workspace_id=%s AND status='present'",
            (workspace,))}
        missing = [p for p, h in wanted.items() if have.get(p) != h]
        if missing:
            raise SyncError(f"{len(missing)} file(s) have not been uploaded yet, e.g. {missing[0]}")
        gone = [p for p in have if p not in wanted and not p.startswith(UPLOADS)]
        if gone:
            c.execute(f"UPDATE {store._t('source_file')} SET status='removed', content=NULL, updated_at=now() "
                      f"WHERE workspace_id=%s AND path = ANY(%s)", (workspace, gone))
        for p, h in wanted.items():
            if p not in before:
                ch.added.append(p)
            elif before[p] != h:
                ch.modified.append(p)
        ch.removed = [p for p in before if p not in wanted]
        c.execute(
            f"""INSERT INTO {store._t('workspace')} (id, synced_state, last_sync_at) VALUES (%s,%s,now())
                ON CONFLICT (id) DO UPDATE SET synced_state=EXCLUDED.synced_state, last_sync_at=now()""",
            (workspace, Jsonb(wanted)))
    return ch


def materialize(store: Store, dest: Path, workspace: str = WORKSPACE) -> int:
    """Write the mirrored files to `dest`, keeping the user's folder structure so every
    extractor that finds files by folder and pattern works unchanged. Only files whose
    content changed are rewritten, and files no longer present are deleted. Returns the
    number of files written."""
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    wrote = 0
    with store.conn() as c:
        meta = list(c.execute(f"SELECT path, sha256 FROM {store._t('source_file')} "
                              f"WHERE workspace_id=%s AND status='present'", (workspace,)))
    keep = set()
    stale = []
    for r in meta:
        target = (dest / r["path"]).resolve()
        if dest not in target.parents:
            raise SyncError(f"path escapes the workspace: {r['path']}")
        keep.add(target)
        if not (target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == r["sha256"]):
            stale.append(r["path"])
    # Only the files that differ are fetched, one at a time: no need to move the whole folder
    # across the network (or hold it in memory) when most of it is already on disk.
    for path in stale:
        with store.conn() as c:
            row = c.execute(f"SELECT content FROM {store._t('source_file')} WHERE workspace_id=%s AND path=%s "
                            f"AND status='present'", (workspace, path)).fetchone()
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

    @property
    def state(self) -> str:
        if len(self.files) >= self.slot.minimum:
            return "ready"
        return "missing" if self.slot.required else "absent"

    def as_dict(self) -> dict:
        return {"id": self.slot.id, "label": self.slot.label, "folder": self.slot.folder,
                "required": self.slot.required, "state": self.state, "count": len(self.files),
                "files": self.files, "modified": self.modified, "unlocks": list(self.slot.unlocks),
                "hint": self.slot.hint}


def coverage_of(paths: list[str], changed: list[str] | None = None) -> list[SlotStatus]:
    changed = changed or []
    return [SlotStatus(s, slot_files(s, paths), slot_files(s, changed)) for s in SLOTS]


def coverage(store: Store, changed: list[str] | None = None,
             workspace: str = WORKSPACE) -> list[SlotStatus]:
    return coverage_of([r["path"] for r in _present(store, workspace)], changed)


def required_missing(cov: list[SlotStatus]) -> list[SlotStatus]:
    return [c for c in cov if c.state == "missing"]


def unplaced(store: Store, workspace: str = WORKSPACE) -> list[str]:
    """Files in the folder that no slot recognises: shown so nothing is silently ignored."""
    return [r["path"] for r in _present(store, workspace) if slot_for_path(r["path"]) is None
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
