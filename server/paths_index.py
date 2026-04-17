"""Cached `git ls-files` for the gobroker repo, used by fuzzy autocomplete.

Refreshes when .git/HEAD mtime changes (i.e. branch switch or pull).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from .config import GOBROKER_PATH

EXTS = (".go", ".py", ".sql", ".yaml", ".yml", ".md", ".sh", ".tf", ".proto")
NAMES = ("CODEOWNERS", "Makefile", "Dockerfile")

_cache: list[str] = []
_cache_key: Optional[float] = None


def _head_mtime() -> Optional[float]:
    head = GOBROKER_PATH / ".git" / "HEAD"
    try:
        return head.stat().st_mtime
    except OSError:
        return None


def list_paths() -> list[str]:
    global _cache, _cache_key
    key = _head_mtime()
    if _cache and key == _cache_key:
        return _cache
    try:
        out = subprocess.run(
            ["git", "ls-files"],
            cwd=str(GOBROKER_PATH),
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.splitlines()
    except subprocess.TimeoutExpired:
        return _cache or []
    filtered = [
        p for p in out
        if p.endswith(EXTS) or Path(p).name in NAMES
    ]
    _cache = filtered
    _cache_key = key
    return _cache
