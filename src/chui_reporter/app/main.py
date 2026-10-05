"""The web application: a thin, authenticated window onto the runtime.

Nothing here decides anything about the report. It moves files in, relays what the runtime
says is happening, passes the user's words and answers back, and serves the rendered report.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from dotenv import find_dotenv, load_dotenv
from fastapi import Body, FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import period as pr
from ..agent.store import Store
from ..runtime import state
from ..runtime.runner import Runtime
from ..workspace import sync as ws_sync
from . import dev as devroutes
from . import pages
from .auth import COOKIE, Auth

STATIC = Path(__file__).parent / "static"
SKIPPED = ("SKIP: The user cannot or will not provide this. Do not ask again. Decide yourself, state the "
           "assumption with report_review_note, and leave out anything that depends on it.")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def create_app(runtime: Runtime | None = None, auth: Auth | None = None) -> FastAPI:
    load_dotenv(find_dotenv(usecwd=True), override=False)
    auth = auth or Auth.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.rt = runtime or Runtime()
        if runtime is None:
            app.state.rt.start()
        yield
        app.state.rt.shutdown()

    app = FastAPI(title="Chui Ventures reporter", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def rt_of(request: Request) -> Runtime:
        return request.app.state.rt

    # ---------------------------------------------------------------- middleware

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
        open_paths = path in ("/healthz", "/api/login", "/") or path.startswith("/static/")
        if not open_paths and not auth.valid(request.cookies.get(COOKIE)):
            return JSONResponse({"error": "login required"}, status_code=401)
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        if path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"           # always revalidate (ETag): a deploy shows up at once
        if path == "/" or path.startswith("/static/"):
            resp.headers.setdefault("Content-Security-Policy",
                                    "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                                    "font-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        return resp

    # ---------------------------------------------------------------- public

    @app.get("/healthz")
    def healthz(request: Request):
        rt_of(request).store.status_counts()          # the database answers
        return {"ok": True}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.post("/api/login")
    async def login(request: Request, response: Response, body: dict = Body(...)):
        who = request.client.host if request.client else "?"
        if auth.throttled(who):
            raise HTTPException(429, "Too many attempts. Wait a minute and try again.")
        if not auth.check(who, str(body.get("password", ""))):
            raise HTTPException(401, "That password is not right.")
        secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
        response.set_cookie(COOKIE, auth.issue(), httponly=True, samesite="lax", secure=secure,
                            max_age=14 * 86400, path="/")
        return {"ok": True}

    @app.post("/api/logout")
    def logout(response: Response):
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/session")
    def session(request: Request):
        return {"ok": True, "auth_required": not auth.disabled}

    # ---------------------------------------------------------------- state

    @app.get("/api/state")
    async def get_state(request: Request):
        return await run_in_threadpool(rt_of(request).snapshot_state)

    @app.get("/api/history")
    async def history(request: Request, n: int = Query(150, le=500)):
        return {"events": await run_in_threadpool(state.recent_events, rt_of(request).store, n)}

    @app.get("/api/events")
    async def events(request: Request, after: int = 0):
        rt = rt_of(request)
        last = int(request.headers.get("last-event-id") or after or 0)

        async def gen():
            nonlocal last
            yield "retry: 2500\n\n"
            quiet = 0
            while not await request.is_disconnected():
                evs = await run_in_threadpool(state.events_after, rt.store, last)
                for e in evs:
                    last = e["id"]
                    yield f"id: {e['id']}\ndata: {json.dumps(e, default=str)}\n\n"
                quiet = 0 if evs else quiet + 1
                if quiet and quiet % 20 == 0:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.6)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---------------------------------------------------------------- folder sync

    def _clean_manifest(raw) -> list[dict]:
        if not isinstance(raw, list) or len(raw) > 5000:
            raise HTTPException(400, "manifest must be a list of at most 5000 files")
        out = []
        for f in raw:
            try:
                path = ws_sync.safe_path(str(f["path"]))
            except (ws_sync.SyncError, KeyError) as exc:
                raise HTTPException(400, f"bad path: {exc}") from exc
            name = path.rsplit("/", 1)[-1]
            if name.startswith((".", "~$")) or name.lower() in ("thumbs.db", "desktop.ini"):
                continue
            sha = str(f.get("sha256", "")).lower()
            if not HEX64.match(sha):
                raise HTTPException(400, f"{path}: missing or malformed sha256")
            out.append({"path": path, "sha256": sha, "size": int(f.get("size", 0)), "mtime": f.get("mtime")})
        return out

    @app.post("/api/sync/plan")
    async def sync_plan(request: Request, body: dict = Body(...)):
        manifest = _clean_manifest(body.get("manifest"))
        need = await run_in_threadpool(ws_sync.plan, rt_of(request).store, manifest)
        too_big = [m["path"] for m in manifest if m["size"] > ws_sync.MAX_FILE_BYTES]
        return {"need": [p for p in need if p not in too_big], "too_large": too_big,
                "limit_mb": ws_sync.MAX_FILE_BYTES // 1_000_000}

    @app.put("/api/sync/file")
    async def sync_file(request: Request, path: str, sha256: str, mtime: float | None = None):
        if not HEX64.match(sha256.lower()):
            raise HTTPException(400, "bad sha256")
        declared = int(request.headers.get("content-length") or 0)
        if declared > ws_sync.MAX_FILE_BYTES:
            raise HTTPException(413, "file too large")
        data = await request.body()
        try:
            changed = await run_in_threadpool(ws_sync.put_file, rt_of(request).store, path, data, sha256.lower(), mtime)
        except ws_sync.SyncError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"stored": changed}

    @app.post("/api/sync/commit")
    async def sync_commit(request: Request, body: dict = Body(...)):
        rt = rt_of(request)
        manifest = _clean_manifest(body.get("manifest"))
        try:
            changes = await run_in_threadpool(ws_sync.commit, rt.store, manifest)
        except ws_sync.SyncError as exc:
            raise HTTPException(409, str(exc)) from exc
        result = await run_in_threadpool(rt.on_sync, changes)
        return {"changes": changes.as_dict(), **result}

    # ---------------------------------------------------------------- run controls

    @app.post("/api/run")
    async def start_run(request: Request, body: dict = Body(default={})):
        return await run_in_threadpool(rt_of(request).request_run, body.get("kind", "update"), body.get("instruction"))

    @app.post("/api/run/resume")
    async def resume_run(request: Request):
        return await run_in_threadpool(rt_of(request).resume_failed)

    @app.post("/api/run/stop")
    async def stop_run(request: Request):
        return await run_in_threadpool(rt_of(request).stop)

    @app.post("/api/steer")
    async def steer(request: Request, body: dict = Body(...)):
        text = str(body.get("text", ""))[:2000]
        return await run_in_threadpool(rt_of(request).steer, text)

    @app.post("/api/questions/{qid}/answer")
    async def answer(request: Request, qid: str, body: dict = Body(...)):
        text = str(body.get("answer", "")).strip()[:2000]
        if body.get("skip"):
            text = SKIPPED
        if not text:
            raise HTTPException(400, "an answer is required")
        res = await run_in_threadpool(rt_of(request).answer, qid, text)
        if not res["ok"]:
            raise HTTPException(409, res["reason"])
        return res

    @app.post("/api/period")
    async def set_period(request: Request, body: dict = Body(...)):
        rt = rt_of(request)
        try:
            p = pr.Period.parse(str(body.get("period", "")))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if state.active_run(rt.store):
            raise HTTPException(409, "Wait for the current run to finish before changing the quarter.")
        await run_in_threadpool(state.update_workspace, rt.store, period=p.code)
        rt.ws_row(fresh=True)
        await run_in_threadpool(state.emit, rt.store, "period", f"Reporting period set to {p.label}")
        return await run_in_threadpool(rt.snapshot_state)

    @app.post("/api/settings")
    async def settings(request: Request, body: dict = Body(...)):
        rt = rt_of(request)
        patch = {k: bool(v) for k, v in body.items() if k in ("auto",)}
        await run_in_threadpool(state.update_workspace, rt.store, settings=patch)
        rt.ws_row(fresh=True)
        return await run_in_threadpool(rt.snapshot_state)

    # ---------------------------------------------------------------- the report

    def _latest(rt: Runtime):
        rs = rt.report_store()
        v = state.latest_version(rs)
        return rs, v

    @app.get("/api/report")
    async def report(request: Request):
        rt = rt_of(request)
        rs, v = await run_in_threadpool(_latest, rt)
        if not v:
            return {"version": None}
        sm = v["summary"]
        sp = sm.get("section_pages", {})
        changed = [k for k in sm.get("changed", []) if k in sp]
        sizes = sm.get("sizes")
        if not sizes:                                   # versions stored before sizes were recorded
            pdf = await run_in_threadpool(state.version_blob, rs, v["version"], "pdf")
            sizes = await run_in_threadpool(pages.page_sizes, pdf) if pdf else []
        return {"version": v["version"], "pages": v["pages"], "created_at": v["created_at"], "sizes": sizes,
                "sections": [{"key": s["key"], "title": s["title"], "page": sp.get(s["key"])}
                             for s in sm.get("sections", [])],
                "changed": sm.get("changed", []),
                "changed_pages": sorted({sp[k] for k in changed}) if v["version"] > 1 else [],
                "versions": await run_in_threadpool(state.versions, rs, 8)}

    @app.get("/api/report/page/{n}.png")
    async def report_page(request: Request, n: int, v: int | None = None, scale: float = 1.6):
        rt = rt_of(request)
        rs = rt.report_store()
        if v is None:
            lv = await run_in_threadpool(state.latest_version, rs)
            if not lv:
                raise HTTPException(404, "no report yet")
            v = lv["version"]
        pdf = await run_in_threadpool(state.version_blob, rs, v, "pdf")
        if not pdf:
            raise HTTPException(404, "no such version")
        scale = max(1.0, min(scale, 3.0))
        try:
            png = await run_in_threadpool(pages.render_page, pdf, n, key=(rs.report_id, v), scale=scale)
        except IndexError as exc:
            raise HTTPException(404, "no such page") from exc
        return Response(png, media_type="image/png",
                        headers={"Cache-Control": "private, max-age=31536000, immutable"})

    @app.get("/api/report/sizes")
    async def report_sizes(request: Request, v: int | None = None):
        rs = rt_of(request).report_store()
        lv = await run_in_threadpool(state.latest_version, rs)
        if not lv:
            raise HTTPException(404, "no report yet")
        pdf = await run_in_threadpool(state.version_blob, rs, v or lv["version"], "pdf")
        return {"version": v or lv["version"], "sizes": await run_in_threadpool(pages.page_sizes, pdf)}

    @app.get("/api/report/download")
    async def download(request: Request, fmt: str = "pdf", v: int | None = None):
        if fmt not in ("pdf", "docx"):
            raise HTTPException(400, "fmt must be pdf or docx")
        rt = rt_of(request)
        rs = rt.report_store()
        lv = await run_in_threadpool(state.latest_version, rs)
        if not lv:
            raise HTTPException(404, "no report yet")
        v = v or lv["version"]
        blob = await run_in_threadpool(state.version_blob, rs, v, fmt)
        if not blob:
            raise HTTPException(404, "not available")
        P = rt.period()
        name = f"Chui Ventures Fund I - {P.label} Investor Report.{fmt}"
        mt = "application/pdf" if fmt == "pdf" else \
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        return Response(blob, media_type=mt, headers={
            "Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "private, no-store"})

    @app.get("/api/notes")
    async def notes(request: Request):
        rs = rt_of(request).report_store()
        rows = await run_in_threadpool(rs.review_notes)
        return {"notes": [{"area": r["area"], "severity": r["severity"], "text": r["text"]} for r in rows]}

    # ---------------------------------------------------------------- brand assets (from the synced Branding folder)

    @app.get("/brand/{path:path}")
    async def brand(request: Request, path: str):
        if not re.search(r"\.(ttf|otf|woff2?|png|svg|jpg|jpeg)$", path, re.I):
            raise HTTPException(404)
        try:
            safe = ws_sync.safe_path("Branding/" + path)
        except ws_sync.SyncError as exc:
            raise HTTPException(404) from exc
        store = rt_of(request).store

        def fetch():
            with store.conn() as c:
                return c.execute(f"SELECT content FROM {store._t('source_file')} WHERE workspace_id='default' "
                                 f"AND path=%s AND status='present'", (safe,)).fetchone()

        row = await run_in_threadpool(fetch)
        if not row:
            raise HTTPException(404)
        ext = safe.rsplit(".", 1)[-1].lower()
        mt = {"ttf": "font/ttf", "otf": "font/otf", "woff": "font/woff", "woff2": "font/woff2", "png": "image/png",
              "svg": "image/svg+xml", "jpg": "image/jpeg", "jpeg": "image/jpeg"}[ext]
        return Response(bytes(row["content"]), media_type=mt, headers={"Cache-Control": "private, max-age=86400"})

    if os.environ.get("CHUI_ENV", "").lower() == "dev":
        devroutes.install(app)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def app_factory() -> FastAPI:
    return create_app()
