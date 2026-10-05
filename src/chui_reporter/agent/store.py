"""The persistently-mutatable report, in Postgres (Neon).

The report is rows, never a file. Sections and table cells are updated
independently; the .docx and .pdf are rendered from these rows on demand and are
disposable build artifacts. The fact ledger sits beside them and is what the
numeric gate checks prose against.

Everything lives in its own schema (default `chui`) so it can never collide with
the LangGraph checkpoint tables in `public` or Neon Auth's `neon_auth`. There is
deliberately no SQLite fallback: a silent fallback hides a misconfigured
DATABASE_URL until the day state is lost.
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


class StoreConfigError(RuntimeError):
    pass


def database_url() -> str:
    url = (os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or "").strip()
    if not url:
        raise StoreConfigError(
            "DATABASE_URL is not set. The report store and agent checkpoints live in "
            "Postgres (Neon); put the connection string in chui-reporter/.env."
        )
    return url


_DDL = """
CREATE SCHEMA IF NOT EXISTS {s};
CREATE TABLE IF NOT EXISTS {s}.report (
    id          TEXT PRIMARY KEY,
    fund        TEXT NOT NULL,
    quarter     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'draft',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS {s}.section (
    report_id   TEXT NOT NULL,
    key         TEXT NOT NULL,
    ord         INTEGER NOT NULL,
    title       TEXT NOT NULL,
    present     BOOLEAN NOT NULL DEFAULT TRUE,
    body        TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (report_id, key)
);
CREATE TABLE IF NOT EXISTS {s}.tbl (
    report_id   TEXT NOT NULL,
    key         TEXT NOT NULL,
    section_key TEXT,
    title       TEXT,
    columns     JSONB NOT NULL,
    rows        JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (report_id, key)
);
CREATE TABLE IF NOT EXISTS {s}.fact (
    id           BIGSERIAL PRIMARY KEY,
    report_id    TEXT NOT NULL,
    label        TEXT NOT NULL,
    value        DOUBLE PRECISION,
    text_value   TEXT,
    unit         TEXT,
    source_file  TEXT,
    source_sheet TEXT,
    source_cell  TEXT,
    as_of        TEXT,
    status       TEXT NOT NULL DEFAULT 'extracted',
    note         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fact_report_label ON {s}.fact (report_id, label);
ALTER TABLE {s}.tbl ADD COLUMN IF NOT EXISTS options JSONB NOT NULL DEFAULT '{{}}'::jsonb;
ALTER TABLE {s}.report ADD COLUMN IF NOT EXISTS meta JSONB NOT NULL DEFAULT '{{}}'::jsonb;
CREATE TABLE IF NOT EXISTS {s}.chart (
    report_id   TEXT NOT NULL,
    key         TEXT NOT NULL,
    section_key TEXT,
    title       TEXT,
    kind        TEXT NOT NULL,
    labels      JSONB NOT NULL,
    values      JSONB NOT NULL,
    options     JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (report_id, key)
);
-- The user's folder, mirrored. The agent runs on a server and cannot see a laptop, so the
-- browser uploads what the user places in their local folder and the server keeps it here.
CREATE TABLE IF NOT EXISTS {s}.source_file (
    workspace_id TEXT NOT NULL DEFAULT 'default',
    path         TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    size         BIGINT NOT NULL,
    mtime        DOUBLE PRECISION,
    content      BYTEA,
    status       TEXT NOT NULL DEFAULT 'present',
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, path)
);
CREATE TABLE IF NOT EXISTS {s}.workspace (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT 'Chui Ventures Fund I',
    period        TEXT NOT NULL DEFAULT '2026Q2',
    last_sync_at  TIMESTAMPTZ,
    synced_state  JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    settings      JSONB NOT NULL DEFAULT '{{}}'::jsonb
);
CREATE TABLE IF NOT EXISTS {s}.run (
    id           TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    thread_id    TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'build',
    instruction  TEXT,
    status       TEXT NOT NULL DEFAULT 'queued',
    error        TEXT,
    worker       TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    nudges       INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    heartbeat_at TIMESTAMPTZ
);
-- One ordered stream for the whole workspace: what the agent is doing, what it asks,
-- what changed. The UI replays it from any point, so a closed tab misses nothing.
CREATE TABLE IF NOT EXISTS {s}.event (
    id           BIGSERIAL PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    run_id       TEXT,
    kind         TEXT NOT NULL,
    label        TEXT NOT NULL DEFAULT '',
    chapter      TEXT,
    detail       JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS event_ws_id ON {s}.event (workspace_id, id);
CREATE TABLE IF NOT EXISTS {s}.question (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'info',
    prompt       TEXT NOT NULL,
    why          TEXT,
    options      JSONB NOT NULL DEFAULT '[]'::jsonb,
    slots        JSONB NOT NULL DEFAULT '[]'::jsonb,
    status       TEXT NOT NULL DEFAULT 'open',
    answer       TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    answered_at  TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS {s}.report_version (
    id           BIGSERIAL PRIMARY KEY,
    report_id    TEXT NOT NULL,
    version      INTEGER NOT NULL,
    pages        INTEGER,
    summary      JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    pdf          BYTEA,
    docx         BYTEA,
    notes        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (report_id, version)
);
-- Anything about the DATA rather than the report: gaps, source disagreements,
-- stale marks, decisions needed. It never appears in the document.
CREATE TABLE IF NOT EXISTS {s}.review_note (
    id          BIGSERIAL PRIMARY KEY,
    report_id   TEXT NOT NULL,
    area        TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'info',
    text        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


# Fact statuses. Only the first two license a figure in the report:
#   extracted   read by code from a source cell/page, or verified against one
#   derived     computed by code from other grounded facts
#   claimed     asserted by the agent and NOT verified -- licenses nothing
#   quarantined a source value that was an Excel error
GROUNDING_STATUSES = ("extracted", "derived")


@dataclass
class Fact:
    label: str
    value: float | None = None
    text_value: str | None = None
    unit: str | None = None
    source_file: str | None = None
    source_sheet: str | None = None
    source_cell: str | None = None
    as_of: str | None = None
    status: str = "extracted"
    note: str | None = None


_POOLS: dict[str, Any] = {}
_POOLS_LOCK = __import__("threading").Lock()


def _pool_for(url: str):
    """One small connection pool per database, shared by every Store in the process.

    Opening a connection per operation costs a TLS handshake each time, which adds up when a
    run narrates every step. The pool validates a connection before lending it, so a dropped
    one (Neon scales to zero, the pooler recycles) is replaced rather than surfacing as an error."""
    with _POOLS_LOCK:
        pool = _POOLS.get(url)
        if pool is None:
            from psycopg_pool import ConnectionPool

            pool = ConnectionPool(
                conninfo=url, min_size=0, max_size=6, max_idle=25, max_lifetime=600, open=True, timeout=30,
                kwargs={"row_factory": dict_row, "prepare_threshold": None, "connect_timeout": 20})
            _POOLS[url] = pool
        return pool


@dataclass
class Store:
    url: str | None = field(default=None, repr=False)  # holds a password: never in a repr
    schema: str = "chui"
    report_id: str = "chui-fund-i"
    _ready: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if not _IDENT.match(self.schema):
            raise StoreConfigError(f"invalid schema name {self.schema!r}")
        self.url = self.url or database_url()

    # -- connection ---------------------------------------------------------

    @contextmanager
    def conn(self):
        """One short-lived connection per operation.

        Neon scales to zero and the pooled endpoint recycles connections, so a
        long-lived handle is a liability. `prepare_threshold=None` keeps this
        compatible with the transaction-mode pooler.
        """
        if not self._ready:
            # Schema creation gets its OWN committed transaction. Doing it inside
            # the caller's transaction meant a failing first operation rolled the
            # DDL back too -- while the process believed the tables existed.
            with psycopg.connect(self.url, autocommit=True, prepare_threshold=None,
                                 connect_timeout=20) as ddl:
                ddl.execute(_DDL.format(s=self.schema))
            self._ready = True
        with _pool_for(self.url).connection() as cx:
            yield cx

    def _t(self, name: str) -> str:
        return f"{self.schema}.{name}"

    def drop_schema(self) -> None:
        """Test cleanup only."""
        with psycopg.connect(self.url) as cx:
            cx.execute(f"DROP SCHEMA IF EXISTS {self.schema} CASCADE")
        self._ready = False

    # -- report -------------------------------------------------------------

    def ensure_report(self, fund: str, quarter: str) -> str:
        with self.conn() as c:
            c.execute(
                f"INSERT INTO {self._t('report')} (id, fund, quarter) VALUES (%s,%s,%s) "
                f"ON CONFLICT (id) DO NOTHING",
                (self.report_id, fund, quarter),
            )
        return self.report_id

    def set_section(self, key: str, title: str, body: str, ord_: int = 0,
                    present: bool = True) -> None:
        with self.conn() as c:
            c.execute(
                f"""INSERT INTO {self._t('section')} (report_id, key, ord, title, present, body)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (report_id, key) DO UPDATE SET
                      title=EXCLUDED.title, body=EXCLUDED.body, ord=EXCLUDED.ord,
                      present=EXCLUDED.present, updated_at=now()""",
                (self.report_id, key, ord_, title, present, body),
            )

    def set_table(self, key: str, title: str, columns: list[str],
                  rows: list[list[Any]], section_key: str | None = None,
                  options: dict | None = None) -> None:
        with self.conn() as c:
            c.execute(
                f"""INSERT INTO {self._t('tbl')}
                      (report_id, key, section_key, title, columns, rows, options)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (report_id, key) DO UPDATE SET
                      title=EXCLUDED.title, columns=EXCLUDED.columns, rows=EXCLUDED.rows,
                      section_key=EXCLUDED.section_key, options=EXCLUDED.options,
                      updated_at=now()""",
                (self.report_id, key, section_key, title, Jsonb(columns), Jsonb(rows),
                 Jsonb(options or {})),
            )

    def set_chart(self, key: str, title: str, kind: str, labels: list[str],
                  values: list[float], section_key: str | None = None,
                  options: dict | None = None) -> None:
        with self.conn() as c:
            c.execute(
                f"""INSERT INTO {self._t('chart')}
                      (report_id, key, section_key, title, kind, labels, values, options)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (report_id, key) DO UPDATE SET
                      title=EXCLUDED.title, kind=EXCLUDED.kind, labels=EXCLUDED.labels,
                      values=EXCLUDED.values, section_key=EXCLUDED.section_key,
                      options=EXCLUDED.options, updated_at=now()""",
                (self.report_id, key, section_key, title, kind, Jsonb(labels),
                 Jsonb(values), Jsonb(options or {})),
            )

    def charts(self) -> dict[str, dict]:
        with self.conn() as c:
            return {r["key"]: r for r in c.execute(
                f"SELECT * FROM {self._t('chart')} WHERE report_id=%s", (self.report_id,))}

    def delete_section(self, key: str) -> None:
        """Remove a section together with the tables and charts attached to it -- leaving
        them behind would orphan them and block the render."""
        with self.conn() as c:
            for table in ("tbl", "chart"):
                c.execute(f"DELETE FROM {self._t(table)} WHERE report_id=%s AND section_key=%s",
                          (self.report_id, key))
            c.execute(f"DELETE FROM {self._t('section')} WHERE report_id=%s AND key=%s",
                      (self.report_id, key))

    # -- report meta (cover fields etc.) ------------------------------------

    def set_meta(self, **kv: Any) -> None:
        with self.conn() as c:
            c.execute(f"UPDATE {self._t('report')} SET meta = meta || %s, updated_at=now() "
                      f"WHERE id=%s", (Jsonb(kv), self.report_id))

    def meta(self) -> dict:
        with self.conn() as c:
            r = c.execute(f"SELECT meta FROM {self._t('report')} WHERE id=%s",
                          (self.report_id,)).fetchone()
            return r["meta"] if r else {}

    # -- completion: has the work actually reached the end? -----------------

    def mark(self, key: str) -> None:
        """Stamp `key` with the database clock, so it compares with updated_at columns."""
        with self.conn() as c:
            c.execute(f"UPDATE {self._t('report')} SET meta = meta || jsonb_build_object(%s::text, now()::text) "
                      f"WHERE id=%s", (key, self.report_id))

    def completion(self) -> dict[str, bool]:
        """Rendered since the last edit; inspected since the last render."""
        with self.conn() as c:
            r = c.execute(
                f"""SELECT (meta->>'rendered_at')::timestamptz AS rendered,
                           (meta->>'inspected_at')::timestamptz AS inspected,
                           GREATEST((SELECT max(updated_at) FROM {self._t('section')} WHERE report_id=%s),
                                    (SELECT max(updated_at) FROM {self._t('tbl')} WHERE report_id=%s),
                                    (SELECT max(updated_at) FROM {self._t('chart')} WHERE report_id=%s)) AS content,
                           (SELECT count(*) FROM {self._t('section')} WHERE report_id=%s AND present) AS n_sections
                    FROM {self._t('report')} WHERE id=%s""",
                (self.report_id,) * 5).fetchone()
        if not r or not r["n_sections"]:
            return {"rendered": False, "inspected": False}
        rendered = r["rendered"] is not None and (r["content"] is None or r["rendered"] >= r["content"])
        inspected = rendered and r["inspected"] is not None and r["inspected"] >= r["rendered"]
        return {"rendered": rendered, "inspected": inspected}

    # -- review notes: about the data, never part of the document -----------

    def add_review_note(self, area: str, text: str, severity: str = "info") -> None:
        with self.conn() as c:
            c.execute(
                f"""INSERT INTO {self._t('review_note')} (report_id, area, severity, text)
                    SELECT %s,%s,%s,%s WHERE NOT EXISTS (
                      SELECT 1 FROM {self._t('review_note')}
                      WHERE report_id=%s AND area=%s AND text=%s)""",
                (self.report_id, area, severity, text, self.report_id, area, text))

    def review_notes(self) -> list[dict]:
        with self.conn() as c:
            return list(c.execute(
                f"SELECT * FROM {self._t('review_note')} WHERE report_id=%s "
                f"ORDER BY CASE severity WHEN 'decision' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, id",
                (self.report_id,)))

    def sections(self) -> list[dict]:
        with self.conn() as c:
            return list(c.execute(
                f"SELECT * FROM {self._t('section')} WHERE report_id=%s AND present "
                f"ORDER BY ord, key", (self.report_id,)))

    def tables(self) -> dict[str, dict]:
        with self.conn() as c:
            return {r["key"]: r for r in c.execute(
                f"SELECT * FROM {self._t('tbl')} WHERE report_id=%s", (self.report_id,))}

    # -- facts --------------------------------------------------------------

    def add_facts(self, facts: list[Fact]) -> int:
        """Idempotent: a LangGraph node re-runs from the top on resume, so the
        same fact arriving twice must not become two rows."""
        if not facts:
            return 0
        with self.conn() as c:
            with c.cursor() as cur:
                cur.executemany(
                    f"""INSERT INTO {self._t('fact')} (report_id,label,value,text_value,unit,
                          source_file,source_sheet,source_cell,as_of,status,note)
                        SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                        WHERE NOT EXISTS (
                          SELECT 1 FROM {self._t('fact')}
                          WHERE report_id=%s AND label=%s
                            AND value IS NOT DISTINCT FROM %s::double precision
                            AND source_file IS NOT DISTINCT FROM %s
                            AND source_sheet IS NOT DISTINCT FROM %s
                            AND source_cell IS NOT DISTINCT FROM %s
                            AND status=%s)""",
                    [(self.report_id, f.label, f.value, f.text_value, f.unit,
                      f.source_file, f.source_sheet, f.source_cell, f.as_of,
                      f.status, f.note,
                      self.report_id, f.label, f.value, f.source_file,
                      f.source_sheet, f.source_cell, f.status) for f in facts],
                )
        return len(facts)

    def grounded_values(self) -> set[float]:
        """Every numeric value the report is allowed to state.

        A fact recorded in thousands or millions (as a source prints it: 7,971 under a
        US$000 heading) licenses both forms, because a reader may meet it as 7,971 in a
        table or as $7.97M in prose. Without this the gate could never accept the prose
        form of a figure whose ledger entry carries a scale."""
        scale = {"USD_thousands": 1e3, "USD_millions": 1e6}
        out: set[float] = set()
        with self.conn() as c:
            for r in c.execute(
                f"SELECT value, unit FROM {self._t('fact')} WHERE report_id=%s "
                f"AND value IS NOT NULL AND status = ANY(%s)",
                (self.report_id, list(GROUNDING_STATUSES))):
                out.add(r["value"])
                if r["unit"] in scale:
                    out.add(r["value"] * scale[r["unit"]])
        return out

    def fact_values(self, labels: list[str]) -> dict[str, float]:
        """Grounded values by label, for computing derived facts."""
        with self.conn() as c:
            rows = c.execute(
                f"SELECT label, value FROM {self._t('fact')} WHERE report_id=%s "
                f"AND label = ANY(%s) AND value IS NOT NULL AND status = ANY(%s) ORDER BY id",
                (self.report_id, labels, list(GROUNDING_STATUSES)))
            return {r["label"]: r["value"] for r in rows}

    def clear_facts(self) -> int:
        with self.conn() as c:
            return c.execute(f"DELETE FROM {self._t('fact')} WHERE report_id=%s",
                             (self.report_id,)).rowcount

    def status_counts(self) -> dict[str, int]:
        with self.conn() as c:
            return {r["status"]: r["n"] for r in c.execute(
                f"SELECT status, count(*) AS n FROM {self._t('fact')} "
                f"WHERE report_id=%s GROUP BY status", (self.report_id,))}

    def find_facts(self, like: str, limit: int = 40) -> list[dict]:
        with self.conn() as c:
            return list(c.execute(
                f"SELECT label,value,unit,source_file,source_sheet,source_cell,status,note "
                f"FROM {self._t('fact')} WHERE report_id=%s AND label ILIKE %s LIMIT %s",
                (self.report_id, f"%{like}%", limit)))

    def fact_count(self) -> int:
        with self.conn() as c:
            return c.execute(
                f"SELECT count(*) AS n FROM {self._t('fact')} WHERE report_id=%s",
                (self.report_id,)).fetchone()["n"]
