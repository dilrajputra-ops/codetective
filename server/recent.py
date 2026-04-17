"""Recently-investigated paths, persisted to a flat JSON file.
Powers the startup gh-cache prewarm. Best-effort: any I/O failure is silent."""
from __future__ import annotations

import json
import threading

from .config import RECENT_PATHS_FILE

_LOCK = threading.Lock()
_MAX_RECENT = 50


def record(path: str) -> None:
    """Push `path` to head of recent list, deduped, capped."""
    if not path:
        return
    with _LOCK:
        try:
            existing = json.loads(RECENT_PATHS_FILE.read_text()) if RECENT_PATHS_FILE.exists() else []
        except Exception:
            existing = []
        existing = [p for p in existing if p != path]
        existing.insert(0, path)
        existing = existing[:_MAX_RECENT]
        try:
            RECENT_PATHS_FILE.parent.mkdir(parents=True, exist_ok=True)
            RECENT_PATHS_FILE.write_text(json.dumps(existing))
        except Exception:
            pass


def top(n: int) -> list[str]:
    try:
        existing = json.loads(RECENT_PATHS_FILE.read_text()) if RECENT_PATHS_FILE.exists() else []
        return existing[:n]
    except Exception:
        return []
