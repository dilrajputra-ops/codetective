"""Background gh-cache prewarming on server startup.

Runs in a daemon thread so it never blocks request serving. For each recent
path, fires off the same gh queries that /investigate uses so the on-disk
cache (`/tmp/codemap-cache`) is hot by the time the user clicks Investigate.
Best-effort: any failure is logged and skipped.
"""
from __future__ import annotations

import sys
import threading

from . import gh_client, recent
from .config import MERGED_WINDOWS_DAYS, PREWARM_TOP_N


def _warm_path(path: str) -> None:
    try:
        gh_client.open_prs_touching(path)
    except Exception as e:
        print(f"[prewarm] open_prs {path}: {e!r}", file=sys.stderr)
    for d in MERGED_WINDOWS_DAYS:
        try:
            gh_client.merged_prs_touching(path, d)
        except Exception as e:
            print(f"[prewarm] merged_prs {path} {d}d: {e!r}", file=sys.stderr)


def _warm_loop() -> None:
    paths = recent.top(PREWARM_TOP_N)
    if not paths:
        return
    print(f"[prewarm] warming gh cache for {len(paths)} recent paths", file=sys.stderr)
    for p in paths:
        _warm_path(p)
    print(f"[prewarm] done", file=sys.stderr)


def kick_off() -> None:
    """Fire-and-forget prewarm in a daemon thread."""
    t = threading.Thread(target=_warm_loop, name="prewarm", daemon=True)
    t.start()
