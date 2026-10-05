"""Development-only helpers. Mounted only when CHUI_ENV=dev, and only for loopback clients.

The folder picker cannot be driven by an automated browser, so in development the front end
can be pointed at a *virtual* folder served from CHUI_SOURCE_ROOT. The browser still walks it,
hashes it, plans, uploads and commits through the same code as a real folder.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

SKIP_TOP = {"chui-reporter", ".git", "build_artifacts", "node_modules", ".venv"}


def _root() -> Path:
    return Path(os.environ.get("CHUI_SOURCE_ROOT") or Path(__file__).resolve().parents[4]).resolve()


def _local(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(403, "development helper")


def install(app: FastAPI) -> None:
    @app.get("/api/dev/tree")
    def tree(request: Request):
        _local(request)
        root, out = _root(), []
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            if not p.is_file() or rel.parts[0] in SKIP_TOP or any(x.startswith(".") for x in rel.parts):
                continue
            st = p.stat()
            out.append({"path": rel.as_posix(), "size": st.st_size, "mtime": st.st_mtime * 1000})
        return {"name": root.name, "files": out}

    @app.get("/api/dev/file")
    def file(request: Request, path: str):
        _local(request)
        root = _root()
        p = (root / path).resolve()
        if root not in p.parents or not p.is_file():
            raise HTTPException(404)
        return Response(p.read_bytes(), media_type="application/octet-stream")
