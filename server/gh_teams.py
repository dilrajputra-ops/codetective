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


# ---------- bulk fetch: every team in the org via GraphQL ----------
#
# Used by the contributors page so every engineer can be tagged with their
# team(s), not just engineers whose team happens to have been touched by a
# recent investigation. One GraphQL call per page of 50 teams + their members.

_TEAMS_QUERY = (
    'query($cursor: String) { organization(login: "%s") { '
    "teams(first: 50, after: $cursor) { "
    "pageInfo { hasNextPage endCursor } "
    "nodes { slug name description url parentTeam { slug } "
    "members(first: 100) { totalCount nodes { login url avatarUrl } } "
    "} } } }"
) % GH_ORG


def _gh_graphql(query: str, cursor: Optional[str] = None, timeout: float = 12.0) -> Optional[dict]:
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    if cursor:
        cmd += ["-f", f"cursor={cursor}"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None


def refresh_all() -> int:
    """Fetch every team in the org with their members, populate the cache.

    Idempotent and safe to call from prewarm. Returns the number of teams
    successfully written. Existing cache entries fetched via lookup() are
    preserved if the bulk fetch fails (we never wipe the cache)."""
    cache = _load_cache()
    cursor: Optional[str] = None
    written = 0
    now = int(time.time())
    for _ in range(20):  # safety bound: org has nowhere near 1000 teams
        page = _gh_graphql(_TEAMS_QUERY, cursor)
        if not page:
            break
        try:
            teams_page = page["data"]["organization"]["teams"]
        except (KeyError, TypeError):
            break
        for t in teams_page.get("nodes") or []:
            slug = t.get("slug")
            if not slug:
                continue
            members_node = t.get("members") or {}
            members = []
            for m in members_node.get("nodes") or []:
                login = (m.get("login") or "").strip()
                if not login:
                    continue
                members.append({
                    "login": login,
                    "html_url": m.get("url") or f"https://github.com/{login}",
                    "avatar_url": m.get("avatarUrl") or f"https://github.com/{login}.png",
                })
            parent = t.get("parentTeam") or {}
            cache[slug] = {
                "slug": slug,
                "name": t.get("name") or slug,
                "description": t.get("description") or "",
                "html_url": t.get("url"),
                "members_count": members_node.get("totalCount") or len(members),
                "members": members,
                "parent_team": parent.get("slug") if isinstance(parent, dict) else None,
                "_fetched_at": now,
            }
            written += 1
        pi = teams_page.get("pageInfo") or {}
        if not pi.get("hasNextPage"):
            break
        cursor = pi.get("endCursor")
    if written:
        _save_cache(cache)
    return written
