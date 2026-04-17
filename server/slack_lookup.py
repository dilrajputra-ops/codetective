"""Static cache lookup for CODEOWNERS team slug -> Slack channels.

Source: slack_channels.json (built by an agent via Slack MCP). The FastAPI
server can't call MCP itself, so this is a flat-file lookup. To refresh, ask
the agent to re-run slack_search_channels for each team and rewrite the file.
"""
from __future__ import annotations

import json
from typing import Optional

from .config import SLACK_CHANNELS_FILE

_cache: Optional[dict] = None


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        data = json.loads(SLACK_CHANNELS_FILE.read_text())
        _cache = {k: v for k, v in data.items() if not k.startswith("_")}
    except (OSError, json.JSONDecodeError):
        _cache = {}
    return _cache


def lookup(team_slug: str) -> dict:
    """Return {primary, alerts, errors, extra[], note} for a slug. Empty dict if unknown."""
    entry = _load().get(team_slug) or {}
    return {
        "primary": entry.get("primary"),
        "alerts": entry.get("alerts"),
        "errors": entry.get("errors"),
        "extra": entry.get("extra") or [],
        "note": entry.get("_note"),
    }
