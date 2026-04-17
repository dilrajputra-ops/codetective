"""Unified routing lookup for a CODEOWNERS team slug.

Merges three sources:
  - owners.json     (manual: on_call, escalation, docs)
  - gh_teams        (auto: name, html_url, members)
  - slack_lookup    (auto from cache: channels)

Every field is optional. UI must handle missing values gracefully.
"""
from __future__ import annotations

import json
from typing import Optional

from . import gh_teams, slack_lookup
from .config import OWNERS_FILE

_overrides: Optional[dict] = None


def _load_overrides() -> dict:
    global _overrides
    if _overrides is not None:
        return _overrides
    try:
        data = json.loads(OWNERS_FILE.read_text())
        _overrides = {k: v for k, v in data.items() if not k.startswith("_")}
    except (OSError, json.JSONDecodeError):
        _overrides = {}
    return _overrides


def lookup(team_slug: str) -> dict:
    """Return merged routing info for a team slug. Always returns a dict (may be sparse)."""
    if not team_slug:
        return {}
    overrides = _load_overrides().get(team_slug, {}) or {}
    gh = gh_teams.lookup(team_slug) or {}
    slack = slack_lookup.lookup(team_slug)

    return {
        "slug": team_slug,
        "team_name": gh.get("name") or _name_from_slug(team_slug),
        "github_url": gh.get("html_url"),
        "github_description": gh.get("description") or "",
        "members_count": gh.get("members_count"),
        "members": gh.get("members", []),
        "parent_team": gh.get("parent_team"),
        "slack": {
            "primary": slack.get("primary"),
            "alerts": slack.get("alerts"),
            "errors": slack.get("errors"),
            "extra": slack.get("extra", []),
            "note": slack.get("note"),
        },
        "on_call": (overrides.get("on_call") or "").strip() or None,
        "escalation": (overrides.get("escalation") or "").strip() or None,
        "docs": (overrides.get("docs") or "").strip() or None,
        "note": overrides.get("_note") or None,
    }


def _name_from_slug(slug: str) -> str:
    s = slug.rsplit("/", 1)[-1]
    return s.replace("-", " ").replace("_", " ").title()
