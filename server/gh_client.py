"""Open-PR lookup via the gh CLI, with on-disk caching."""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path

from .config import CACHE_DIR, CACHE_TTL_SECONDS, GH_REPO


def _cache_get(key: str):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    f = CACHE_DIR / (hashlib.sha1(key.encode()).hexdigest() + ".json")
    if not f.exists():
        return None
    if time.time() - f.stat().st_mtime > CACHE_TTL_SECONDS:
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


def open_prs_touching(path: str) -> dict:
    """Return {prs: [...], degraded: bool, error: str|None}. Never raises."""
    cache_key = f"open_prs:{GH_REPO}:{path}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    result = {"prs": [], "degraded": False, "error": None}
    try:
        # Search PRs mentioning the path
        q = f'repo:{GH_REPO} is:pr is:open {path}'
        raw = _gh(
            ["api", "-X", "GET", "search/issues", "-f", f"q={q}", "--jq", ".items[] | {number, title, html_url, user: .user.login, updated_at}"],
            timeout=8,
        )
        prs = []
        for line in raw.strip().splitlines():
            if not line.strip():
                continue
            try:
                prs.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        # Enrich with branch name (best effort, capped)
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
