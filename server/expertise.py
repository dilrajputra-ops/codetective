"""DOK-lite contributor expertise scoring.

Implements Fritz et al. 2014 (Degree-of-Knowledge) minus the IDE-interaction term:
    score = blame_share*Wb + recency*Wr + authorship*Wa + change_volume*Wv - departed*Wd

Recency uses a 6-month half-life, capped to keep one prolific author from dominating.
Departed contributors are still returned so the UI can show them greyed; the large
negative weight ensures they never rank above active people.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

BLAME_W = 1.0
RECENCY_W = 1.5
AUTHOR_W = 0.8
VOLUME_W = 0.5
DEPARTED_W = 5.0

RECENCY_HALF_LIFE_MONTHS = 6.0
RECENCY_CAP = 3.0


def _identity_key(name: str, email: str) -> str:
    return (email or name or "").lower().strip()


def _is_departed(name: str, email: str, patterns: list[str]) -> bool:
    hay = f"{name or ''}\t{email or ''}".lower()
    return any(p in hay for p in patterns)


def _months_since(iso: str, now: datetime) -> Optional[float]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    delta_days = (now - dt).total_seconds() / 86400.0
    return delta_days / 30.4375


def _recency_term(commit_dates: list[str], now: datetime) -> float:
    total = 0.0
    for d in commit_dates:
        m = _months_since(d, now)
        if m is None or m < 0:
            continue
        total += 0.5 ** (m / RECENCY_HALF_LIFE_MONTHS)
    return min(total, RECENCY_CAP)


def _is_non_trivial(commit: dict) -> bool:
    """Skip commits with zero net change (likely rename-only after `--no-merges`)."""
    return (commit.get("lines_added", 0) + commit.get("lines_deleted", 0)) > 0


def score_contributors(
    blame_authors: list[dict],
    commits_with_stats: list[dict],
    first_author: dict,
    departed_patterns: list[str],
    open_pr_authors: set[str],
    now: datetime,
) -> list[dict]:
    """Rank contributors of a path using DOK-lite. Returns list sorted by score DESC."""
    total_blame = sum(b.get("lines", 0) for b in blame_authors) or 1
    by_id: dict[str, dict] = {}

    for b in blame_authors:
        k = _identity_key(b.get("name", ""), b.get("email", ""))
        if not k:
            continue
        by_id[k] = {
            "name": b.get("name", ""),
            "email": b.get("email", ""),
            "lines": b.get("lines", 0),
            "blame_share": b.get("lines", 0) / total_blame,
            "commit_dates": [],
            "non_trivial_lines": 0,
            "last_active": b.get("last_date", ""),
        }

    for c in commits_with_stats:
        k = _identity_key(c.get("author", ""), c.get("email", ""))
        if not k:
            continue
        rec = by_id.setdefault(k, {
            "name": c.get("author", ""),
            "email": c.get("email", ""),
            "lines": 0,
            "blame_share": 0.0,
            "commit_dates": [],
            "non_trivial_lines": 0,
            "last_active": "",
        })
        rec["commit_dates"].append(c.get("date", ""))
        if _is_non_trivial(c):
            rec["non_trivial_lines"] += c.get("lines_added", 0)
        if c.get("date", "") > rec["last_active"]:
            rec["last_active"] = c.get("date", "")

    fa_key = ""
    if first_author:
        fa_key = _identity_key(first_author.get("name", ""), first_author.get("email", ""))

    out = []
    for k, rec in by_id.items():
        recency = _recency_term(rec["commit_dates"], now)
        volume = math.log10(1 + max(0, rec["non_trivial_lines"]))
        is_first = bool(fa_key) and k == fa_key
        is_dep = _is_departed(rec["name"], rec["email"], departed_patterns)

        score = (
            rec["blame_share"] * BLAME_W
            + recency * RECENCY_W
            + (1.0 if is_first else 0.0) * AUTHOR_W
            + volume * VOLUME_W
            - (1.0 if is_dep else 0.0) * DEPARTED_W
        )

        if is_dep:
            role = "Departed"
        elif is_first:
            role = "Created the file"
        elif rec["name"] in open_pr_authors:
            role = "Open PR owner"
        elif recency >= 1.0:
            role = "Active contributor"
        else:
            role = "Historical author"

        out.append({
            "name": rec["name"],
            "email": rec["email"],
            "lines": rec["lines"],
            "last_active": rec["last_active"],
            "score": score,
            "score_breakdown": {
                "blame_share": round(rec["blame_share"], 3),
                "recency": round(recency, 2),
                "authorship": 1.0 if is_first else 0.0,
                "volume": round(volume, 2),
                "departed": is_dep,
            },
            "role": role,
            "is_departed": is_dep,
        })

    out.sort(key=lambda r: (-r["score"], -r["lines"]))
    return out
