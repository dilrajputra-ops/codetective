"""Departed-engineer lookup. Reads departed.txt, matches against blame author
name and email. Substring, case-insensitive. Cached in-memory with mtime check.
"""
from __future__ import annotations

import os
from typing import Literal

from .config import DEPARTED_FILE

Status = Literal["active", "departed", "unknown"]

_patterns: list[str] = []
_mtime: float = 0.0


def _load() -> list[str]:
    global _patterns, _mtime
    try:
        st = os.stat(DEPARTED_FILE)
    except OSError:
        _patterns = []
        _mtime = 0.0
        return _patterns
    if st.st_mtime == _mtime and _patterns:
        return _patterns
    out: list[str] = []
    try:
        for raw in DEPARTED_FILE.read_text().splitlines():
            line = raw.strip().lower()
            if not line or line.startswith("#"):
                continue
            out.append(line)
    except OSError:
        pass
    _patterns = out
    _mtime = st.st_mtime
    return _patterns


def status(name: str = "", email: str = "") -> Status:
    """Return 'departed' if any pattern matches name OR email, else 'active'.
    Returns 'unknown' only when both inputs are empty.
    """
    if not name and not email:
        return "unknown"
    haystack = f"{name}\n{email}".lower()
    for p in _load():
        if p in haystack:
            return "departed"
    # We don't have a positive "active" source; absence from departed list = active.
    return "active"
