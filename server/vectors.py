"""Local vector index over gobroker file paths.

Embeds each tracked path + its CODEOWNERS team using Ollama (`nomic-embed-text`)
and stores vectors in a single SQLite table. Brute-force cosine similarity is
fine for the ~10-30k paths in gobroker; no extra deps needed.

Index is built once on demand (`/reindex` endpoint or first query) and cached
on disk at `VECTOR_DB`. Re-running is idempotent.
"""
from __future__ import annotations

import json
import math
import sqlite3
import struct
import subprocess
import urllib.request
from pathlib import Path
from typing import Iterable

from . import codeowners
from .config import GOBROKER_PATH, OLLAMA_EMBED_MODEL, OLLAMA_HOST, VECTOR_DB


def _conn() -> sqlite3.Connection:
    VECTOR_DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(VECTOR_DB))
    c.execute(
        "CREATE TABLE IF NOT EXISTS paths ("
        "  path TEXT PRIMARY KEY,"
        "  team TEXT,"
        "  vec  BLOB"
        ")"
    )
    return c


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _embed(text: str, timeout: float = 15.0) -> list[float] | None:
    body = json.dumps({"model": OLLAMA_EMBED_MODEL, "prompt": text}).encode()
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/embeddings",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            v = data.get("embedding") or []
            return [float(x) for x in v] if v else None
    except Exception:
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _git_tracked_paths(extensions: tuple[str, ...] = (".go", ".py", ".sql")) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=str(GOBROKER_PATH),
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.splitlines()
    return [p for p in out if p.endswith(extensions)]


def _doc_for_path(path: str) -> str:
    """Embed a short, ownership-flavored description of the file. Cheap and effective."""
    owners = codeowners.match(path)
    team = ", ".join(owners["owners"]) if owners["owners"] else "unowned"
    parts = path.split("/")
    return f"path: {path}\ndirs: {' / '.join(parts[:-1])}\nfile: {parts[-1]}\nowners: {team}"


def index_size() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM paths").fetchone()[0]


def reindex(limit: int | None = None, batch_log_every: int = 200) -> dict:
    """Embed all tracked code paths. Skips ones already in the DB. Returns stats."""
    paths = _git_tracked_paths()
    if limit:
        paths = paths[:limit]

    new = skipped = failed = 0
    with _conn() as c:
        existing = {r[0] for r in c.execute("SELECT path FROM paths").fetchall()}
        for i, p in enumerate(paths, 1):
            if p in existing:
                skipped += 1
                continue
            v = _embed(_doc_for_path(p))
            if not v:
                failed += 1
                continue
            owners = codeowners.match(p)
            team = ",".join(owners["owners"]) if owners["owners"] else ""
            c.execute(
                "INSERT OR REPLACE INTO paths(path, team, vec) VALUES (?,?,?)",
                (p, team, _pack(v)),
            )
            new += 1
            if new % batch_log_every == 0:
                c.commit()
        c.commit()

    return {"total_tracked": len(paths), "new": new, "skipped": skipped, "failed": failed, "indexed": index_size()}


def similar(path: str, k: int = 5) -> list[dict]:
    """Find k most-similar tracked paths to `path`. Excludes the query itself."""
    if index_size() == 0:
        return []
    qvec = _embed(_doc_for_path(path))
    if not qvec:
        return []
    rows: list[tuple[str, str, list[float]]] = []
    with _conn() as c:
        for p, team, blob in c.execute("SELECT path, team, vec FROM paths"):
            rows.append((p, team, _unpack(blob)))
    scored = [
        (p, team, _cosine(qvec, v)) for (p, team, v) in rows if p != path
    ]
    scored.sort(key=lambda r: -r[2])
    return [
        {"path": p, "team": team, "score": round(score, 3)}
        for (p, team, score) in scored[:k]
    ]
