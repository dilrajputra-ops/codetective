"""GitHub team metadata via `gh api`. Cached on disk for 24h.

Returns the `name`, `description`, `html_url`, member count, member list, and
parent team for an `@org/team` slug. Used to enrich routing data in the UI and
to feed current-team-member context to the LLM.
"""
from __future__ import annotations

import json
import subprocess
import time
from typing import Optional

from .config import GH_ORG, GH_TEAMS_CACHE, GH_TEAMS_TTL_SECONDS


def _slug(team_slug: str) -> Optional[str]:
    """'@alpacahq/payments' -> 'payments'. Returns None if it's not our org."""
    if not team_slug or not team_slug.startswith("@"):
        return None
    org_team = team_slug[1:]
    if "/" not in org_team:
        return None
    org, slug = org_team.split("/", 1)
    if org.lower() != GH_ORG.lower():
        return None
    return slug


def _load_cache() -> dict:
    if not GH_TEAMS_CACHE.exists():
        return {}
    try:
        return json.loads(GH_TEAMS_CACHE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        GH_TEAMS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        GH_TEAMS_CACHE.write_text(json.dumps(cache, indent=2))
    except OSError:
        pass


def _gh_api(path: str, timeout: float = 8.0) -> Optional[dict | list]:
    try:
        out = subprocess.run(
            ["gh", "api", path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if out.returncode != 0:
            return None
        return json.loads(out.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None


def _fetch(slug: str) -> Optional[dict]:
    team = _gh_api(f"orgs/{GH_ORG}/teams/{slug}")
    if not isinstance(team, dict):
        return None
    members_raw = _gh_api(f"orgs/{GH_ORG}/teams/{slug}/members?per_page=100") or []
    members = []
    if isinstance(members_raw, list):
        for m in members_raw:
            if isinstance(m, dict) and m.get("login"):
                members.append({
                    "login": m["login"],
                    "html_url": m.get("html_url", f"https://github.com/{m['login']}"),
                    "avatar_url": m.get("avatar_url", f"https://github.com/{m['login']}.png"),
                })
    parent = team.get("parent") or {}
    return {
        "slug": slug,
        "name": team.get("name", slug),
        "description": team.get("description") or "",
        "html_url": team.get("html_url"),
        "members_count": team.get("members_count", len(members)),
        "members": members,
        "parent_team": parent.get("slug") if isinstance(parent, dict) else None,
        "_fetched_at": int(time.time()),
    }


def lookup(team_slug: str) -> Optional[dict]:
    """Return cached team info, fetching if missing or stale."""
    slug = _slug(team_slug)
    if not slug:
        return None
    cache = _load_cache()
    entry = cache.get(slug)
    now = int(time.time())
    if entry and now - entry.get("_fetched_at", 0) < GH_TEAMS_TTL_SECONDS:
        return entry
    fresh = _fetch(slug)
    if fresh:
        cache[slug] = fresh
        _save_cache(cache)
        return fresh
    # Fall back to stale cache rather than returning nothing.
    return entry
