"""Org-wide GitHub roster: login <-> display name for every alpacahq member.

Used as a fallback when extracting GitHub handles for contributors. The
per-team roster in gh_teams.py only has members for ONE team (the codeowner),
so contributors on other teams — very common in a monorepo — never get
matched. This module fetches every org member in ~6s via GraphQL (3 paged
calls of 100), caches for 7 days on disk, and exposes name/email-prefix
lookup helpers.

Scale note: org has ~217 members. REST would need 1+217 calls; GraphQL
gets it in 3. Refresh weekly is plenty — people don't change usernames often.
"""
from __future__ import annotations

import json
import subprocess
import time
from typing import Optional

from .config import CACHE_DIR, GH_ORG

ROSTER_CACHE = CACHE_DIR / "gh-roster.json"
ROSTER_TTL = 7 * 24 * 60 * 60
_GRAPHQL_TIMEOUT = 15


_QUERY = (
    'query($cursor: String) { organization(login: "%s") { '
    "membersWithRole(first: 100, after: $cursor) { "
    "totalCount nodes { login name } pageInfo { hasNextPage endCursor } "
    "} } }"
) % GH_ORG


def _fetch_page(cursor: Optional[str]) -> Optional[dict]:
    cmd = ["gh", "api", "graphql", "-f", f"query={_QUERY}"]
    if cursor:
        cmd += ["-f", f"cursor={cursor}"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=_GRAPHQL_TIMEOUT)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None


def _build_roster() -> list[dict]:
    """Page through the whole org. Returns [] on any failure (fail-silent)."""
    members: list[dict] = []
    cursor: Optional[str] = None
    for _ in range(10):  # safety bound at 1000 members
        page = _fetch_page(cursor)
        if not page:
            return []
        try:
            mw = page["data"]["organization"]["membersWithRole"]
        except (KeyError, TypeError):
            return []
        for n in mw.get("nodes") or []:
            login = (n.get("login") or "").strip()
            if not login:
                continue
            members.append({"login": login, "name": (n.get("name") or "").strip()})
        pi = mw.get("pageInfo") or {}
        if not pi.get("hasNextPage"):
            break
        cursor = pi.get("endCursor")
    return members


def _load_cache() -> dict | None:
    if not ROSTER_CACHE.exists():
        return None
    try:
        data = json.loads(ROSTER_CACHE.read_text())
        if time.time() - data.get("_fetched_at", 0) > ROSTER_TTL:
            return None
        return data
    except (OSError, json.JSONDecodeError):
        return None


def _save_cache(members: list[dict]) -> None:
    try:
        ROSTER_CACHE.parent.mkdir(parents=True, exist_ok=True)
        ROSTER_CACHE.write_text(json.dumps({
            "_fetched_at": int(time.time()),
            "members": members,
        }))
    except OSError:
        pass


_memo: dict | None = None
_indexes: dict | None = None


def _normalize(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _build_indexes(members: list[dict]) -> dict:
    """Pre-build lookup tables. Runs once per process after roster load."""
    by_name_norm: dict[str, str] = {}
    by_login_norm: dict[str, str] = {}
    for m in members:
        login = m.get("login") or ""
        if not login:
            continue
        by_login_norm[_normalize(login)] = login
        nm = _normalize(m.get("name") or "")
        if nm and nm not in by_name_norm:
            by_name_norm[nm] = login
    return {"by_name": by_name_norm, "by_login": by_login_norm}


def _ensure_loaded() -> None:
    global _memo, _indexes
    if _memo is not None:
        return
    cached = _load_cache()
    if cached:
        _memo = cached
    else:
        members = _build_roster()
        if members:
            _save_cache(members)
            _memo = {"members": members, "_fetched_at": int(time.time())}
        else:
            _memo = {"members": [], "_fetched_at": 0}
    _indexes = _build_indexes(_memo.get("members") or [])


def refresh() -> int:
    """Force-refetch the roster. Used by prewarm on startup. Returns member count."""
    global _memo, _indexes
    members = _build_roster()
    if members:
        _save_cache(members)
        _memo = {"members": members, "_fetched_at": int(time.time())}
        _indexes = _build_indexes(members)
    return len(members) if members else 0


def find_login(name: str = "", email: str = "") -> str:
    """Resolve a commit author to a GitHub login using org roster.
    Empty string if no confident match. Order of precedence:
      1. Exact normalized name match ('Brandon Meyerowitz' -> 'brandonmeyerowitz')
      2. Email local-part normalized matches a login ('brandon.meyerowitz' -> 'brandonmeyerowitz')
      3. Email local-part normalized matches a member's name"""
    _ensure_loaded()
    if not _indexes:
        return ""
    by_name = _indexes["by_name"]
    by_login = _indexes["by_login"]

    n = _normalize(name)
    if n and n in by_name:
        return by_name[n]

    em = (email or "").strip().lower()
    if "@" in em:
        local = _normalize(em.split("@", 1)[0])
        if local and local in by_login:
            return by_login[local]
        if local and local in by_name:
            return by_name[local]
    return ""
