"""FastAPI app for Codemap."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import git_ops, paths_index, pr as pr_mod, prewarm, recent, synth, vectors
from .config import GOBROKER_PATH, GH_REPO

app = FastAPI(title="Codemap")

ROOT = Path(__file__).resolve().parents[1]


@app.on_event("startup")
def _warm_on_startup():
    prewarm.kick_off()


class InvestigateBody(BaseModel):
    path: str
    range: str | None = None


@app.get("/health")
def health():
    return {
        "ok": True,
        "gobroker": str(GOBROKER_PATH),
        "gobroker_exists": GOBROKER_PATH.exists(),
        "gh_repo": GH_REPO,
        "vector_index": vectors.index_size(),
    }


@app.post("/reindex")
def reindex(limit: int | None = None):
    return vectors.reindex(limit=limit)


@app.get("/paths")
def paths():
    return {"paths": paths_index.list_paths()}


@app.get("/file")
def file(path: str):
    try:
        return git_ops.read_file(path)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.post("/investigate")
def investigate(body: InvestigateBody):
    path = body.path.strip().lstrip("/")
    if not path:
        raise HTTPException(400, "path is required")
    try:
        case = synth.investigate(path, body.range)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    recent.record(path)
    return JSONResponse(case)


@app.get("/investigate/stream")
def investigate_stream(path: str, range: str | None = None, fp: str | None = None):
    """SSE-style staged investigation. Each `data:` event is a JSON partial case
    with a `stage` key (shell|contributors|github|narrative). Final `event: done`
    signals end of stream so the client can close without triggering EventSource
    auto-reconnect.

    If `fp` matches the server's cheap fingerprint for `path`, short-circuits to
    a single `stage:"unchanged"` event so the client can keep its cached result."""
    p = (path or "").strip().lstrip("/")
    if not p:
        raise HTTPException(400, "path is required")

    def event_stream():
        try:
            if fp:
                current_fp = synth.fingerprint(p)
                if current_fp and current_fp == fp:
                    yield f"data: {json.dumps({'stage':'unchanged','fingerprint':current_fp})}\n\n"
                    yield "event: done\ndata: {}\n\n"
                    recent.record(p)
                    return
            for partial in synth.investigate_stream(p, range):
                yield f"data: {json.dumps(partial)}\n\n"
            recent.record(p)
        except FileNotFoundError as e:
            yield f"event: error\ndata: {json.dumps({'detail': str(e), 'code': 404})}\n\n"
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'detail': f'{type(e).__name__}: {e}', 'code': 500})}\n\n"
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/pr/{ident:path}")
def pr_investigate(ident: str):
    """PR-mode investigation. `ident` may be a PR number, GitHub PR URL, or Jira key."""
    case = pr_mod.investigate(ident)
    if case.get("error"):
        raise HTTPException(400, case["error"])
    return JSONResponse(case)


@app.get("/")
def index():
    return FileResponse(ROOT / "index.html")
