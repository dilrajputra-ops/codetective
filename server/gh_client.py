"""PR lookup via the gh CLI, with on-disk caching.

Two flavors:
- open_prs_touching(path): currently-open PRs mentioning the path.
- merged_prs_touching(path, days): PRs merged in the last N days mentioning the path.

Both are best-effort: any failure returns degraded=True with an error string,
never raises.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from datetime import date, timedelta

from .config import CACHE_DIR, CACHE_TTL_SECONDS, GH_REPO, MERGED_PR_TTL_SECONDS


def _cache_get(key: str, ttl: int = CACHE_TTL_SECONDS):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    f = CACHE_DIR / (hashlib.sha1(key.encode()).hexdigest() + ".json")
    if not f.exists():
        return None
    if time.time() - f.stat().st_mtime > ttl:
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None


def _cache_put(key: str, data):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    f = CACHE_DIR / (hashlib.sha1(key.encode()).hexdigest() + ".json")
    f.write_text(json.dumps(data))


def _gh(args: list[str], timeout: int = 8) -> str:
    out = subprocess.run(
        ["gh"] + args, capture_output=True, text=True, timeout=timeout
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "gh failed")
    return out.stdout


def _search_prs(query: str, date_field: str = "updated_at", limit: int = 10) -> list[dict]:
    """Run `gh api search/issues` with `query`, return a list of normalized PR dicts.
    `date_field` controls which timestamp ends up in the `updated_at` slot
    (so callers can pass 'closed_at' for merged PRs).
    """
    raw = _gh(
        [
            "api", "-X", "GET", "search/issues",
            "-f", f"q={query}",
            "--jq", ".items[] | {number, title, html_url, user: .user.login, updated_at, closed_at, pull_request: .pull_request}",
        ],
        timeout=8,
    )
    out = []
    for line in raw.strip().splitlines():
        if not line.strip():
            continue
        try:
            it = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Prefer the requested date field, fall back to updated_at, then closed_at.
        ts = it.get(date_field) or it.get("updated_at") or it.get("closed_at") or ""
        out.append({
            "number": it.get("number"),
            "title": it.get("title", ""),
            "user": it.get("user", ""),
            "html_url": it.get("html_url", ""),
            "updated_at": ts,
        })
        if len(out) >= limit:
            break
    return out


def open_prs_touching(path: str) -> dict:
    """Return {prs: [...], degraded: bool, error: str|None}. Never raises."""
    cache_key = f"open_prs:{GH_REPO}:{path}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    result = {"prs": [], "degraded": False, "error": None}
    try:
        prs = _search_prs(f"repo:{GH_REPO} is:pr is:open {path}", date_field="updated_at", limit=10)
        # Enrich with branch name (best-effort, capped at 5).
        enriched = []
        for pr in prs[:5]:
            try:
                view = _gh(
                    ["pr", "view", str(pr["number"]), "-R", GH_REPO,
                     "--json", "number,title,author,updatedAt,url,headRefName"],
                    timeout=5,
                )
                v = json.loads(view)
                enriched.append({
                    "number": v["number"],
                    "title": v["title"],
                    "author": (v.get("author") or {}).get("login", pr.get("user", "")),
                    "updated_at": v.get("updatedAt", pr.get("updated_at", "")),
                    "url": v.get("url", pr.get("html_url", "")),
                    "branch": v.get("headRefName", ""),
                })
            except Exception:
                enriched.append({
                    "number": pr.get("number"),
                    "title": pr.get("title", ""),
                    "author": pr.get("user", ""),
                    "updated_at": pr.get("updated_at", ""),
                    "url": pr.get("html_url", ""),
                    "branch": "",
                })
        result["prs"] = enriched
    except subprocess.TimeoutExpired:
        result["degraded"] = True
        result["error"] = "gh timeout"
    except Exception as e:
        result["degraded"] = True
        result["error"] = str(e)[:200]

    _cache_put(cache_key, result)
    return result


def merged_prs_touching(path: str, days: int) -> dict:
    """Return {prs: [...], count: int, days: int, degraded, error}.
    PRs merged in the last `days` days that mention `path`. Cached separately
    per (path, days) so 30d/90d don't collide.
    """
    cache_key = f"merged_prs:{GH_REPO}:{path}:{days}"
    cached = _cache_get(cache_key, ttl=MERGED_PR_TTL_SECONDS)
    if cached is not None:
        return cached

    result = {"prs": [], "count": 0, "days": days, "degraded": False, "error": None}
    try:
        since = (date.today() - timedelta(days=days)).isoformat()
        # `is:merged` includes the merged_at filter; `merged:>=DATE` further restricts.
        # We don't enrich with branch names — merged PRs don't need that detail.
        prs = _search_prs(
            f"repo:{GH_REPO} is:pr is:merged merged:>={since} {path}",
            date_field="closed_at",
            limit=20,
        )
        # Normalize to the same shape the UI expects from open PRs (minus branch).
        result["prs"] = [
            {
                "number": p["number"],
                "title": p["title"],
                "author": p["user"],
                "updated_at": p["updated_at"],  # actually merged_at via closed_at
                "url": p["html_url"],
                "branch": "",
            }
            for p in prs
        ]
        result["count"] = len(result["prs"])
    except subprocess.TimeoutExpired:
        result["degraded"] = True
        result["error"] = "gh timeout"
    except Exception as e:
        result["degraded"] = True
        result["error"] = str(e)[:200]

    _cache_put(cache_key, result)
    return result
