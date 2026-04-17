"""FastAPI app for Codemap."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import git_ops, paths_index, synth, vectors
from .config import GOBROKER_PATH, GH_REPO

app = FastAPI(title="Codemap")

ROOT = Path(__file__).resolve().parents[1]


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
    return JSONResponse(case)


@app.get("/")
def index():
    return FileResponse(ROOT / "index.html")
