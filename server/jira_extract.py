"""Extract Jira ticket IDs from PR/branch/commit text."""
from __future__ import annotations

import re

JIRA_RE = re.compile(r"\b([A-Z]{2,8}-\d+)\b")
JIRA_BASE = "https://alpaca.atlassian.net/browse/"


def extract(sources: list[dict]) -> list[dict]:
    """sources: list of {text, where} -> list of {id, url, where} (deduped, source order)."""
    seen: dict[str, dict] = {}
    for s in sources:
        for m in JIRA_RE.finditer(s.get("text", "") or ""):
            jid = m.group(1)
            if jid not in seen:
                seen[jid] = {"id": jid, "url": JIRA_BASE + jid, "where": s.get("where", "")}
    return list(seen.values())[:5]
