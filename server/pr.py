"""PR-mode investigation: aggregate per-file Codetective signals into PR-shaped triage info.

Reuses every existing primitive (codeowners, git blame, expertise, gh PR search,
employees, owners_map). Adds three things on top:
  1. Per-file "lite" investigation (no LLM, no merged-PRs) so we can fan out across
     a 20-file PR without 80s of Ollama calls.
  2. Reviewer aggregation: sum DOK scores per author across changed files, mark
     active/departed/still-on-team.
  3. Risk flags: stale, departed reviewers, cross-team, conflicts with other open PRs.

Design contract for callers:
  - investigate(ident) is the single entry point. ident may be a PR number ("9821"),
    a GitHub PR URL, or a Jira key ("LPCD-1560").
  - Always returns a dict; never raises (errors land in case["error"]).
  - Cached on disk by (PR number, head SHA) so amends don't get stale answers but
    cache hits are ~free.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Optional

from . import codeowners, employees, expertise, gh_client, git_ops, owners_map
from .config import CACHE_DIR, GH_REPO

_PR_CACHE_TTL = 600  # 10m — PRs change often via pushes/reviews
_PR_NUMBER_RE = re.compile(r"^\d+$")
_PR_URL_RE = re.compile(r"github\.com/[^/]+/[^/]+/pull/(\d+)")
_JIRA_RE = re.compile(r"^[A-Z]{2,}-\d+$")


# ---------- Input parsing ----------

def parse_ident(raw: str) -> dict:
    """Resolve raw input to either a PR number or a search query.

    Returns one of:
      {"kind": "number", "value": int}
      {"kind": "jira", "value": "LPCD-1560"}
      {"kind": "invalid", "value": raw}
    Caller must call resolve_to_number() to actually hit gh for the jira case.
    """
    s = (raw or "").strip().lstrip("#")
    if not s:
        return {"kind": "invalid", "value": raw}
    if _PR_NUMBER_RE.match(s):
        return {"kind": "number", "value": int(s)}
    m = _PR_URL_RE.search(s)
    if m:
        return {"kind": "number", "value": int(m.group(1))}
    if _JIRA_RE.match(s):
        return {"kind": "jira", "value": s}
    return {"kind": "invalid", "value": raw}


def resolve_jira_to_pr(jira_key: str) -> Optional[int]:
    """Search PRs whose title contains the Jira key. Returns the most recent match."""
    try:
        out = subprocess.run(
            ["gh", "pr", "list", "-R", GH_REPO, "--state", "all", "--search", jira_key,
             "--json", "number,updatedAt", "--limit", "5"],
            capture_output=True, text=True, timeout=8,
        )
        if out.returncode != 0:
            return None
        prs = json.loads(out.stdout or "[]")
        if not prs:
            return None
        prs.sort(key=lambda p: p.get("updatedAt", ""), reverse=True)
        return int(prs[0]["number"])
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError, KeyError):
        return None


# ---------- gh fetch ----------

def fetch_pr(pr_number: int) -> dict:
    """Pull the PR + its file list via `gh pr view`. Raises on failure."""
    fields = ",".join([
        "number", "title", "body", "url", "state", "isDraft", "additions", "deletions",
        "createdAt", "updatedAt", "mergedAt", "closedAt",
        "author", "headRefName", "headRefOid", "baseRefName",
        "files", "reviewRequests", "reviews",
    ])
    out = subprocess.run(
        ["gh", "pr", "view", str(pr_number), "-R", GH_REPO, "--json", fields],
        capture_output=True, text=True, timeout=12,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"gh pr view {pr_number} failed")
    return json.loads(out.stdout)


# ---------- Per-file lite investigation ----------

def investigate_file_lite(path: str) -> dict:
    """Codetective signals for one file, minus LLM/merged/jira.

    Optimized for PR mode where we run this across N files in parallel:
      - codeowners (with parent inference)
      - blame + commits_with_stats + first_author for DOK scoring
      - open PRs touching the path (for conflict detection)
    Returns {} for paths missing in the index — caller filters those out.
    """
    if not git_ops.file_exists(path):
        return {"path": path, "missing": True}

    with ThreadPoolExecutor(max_workers=5) as ex:
        f_owners = ex.submit(codeowners.match_with_inference, path)
        f_blame = ex.submit(git_ops.blame, path, None, None)
        f_stats = ex.submit(git_ops.commits_with_stats, path, 50)
        f_first = ex.submit(git_ops.first_author, path)
        f_open = ex.submit(gh_client.open_prs_touching, path)

    owners_match = _safe(f_owners, {"owners": [], "inferred": False, "inferred_from": None})
    blame_data = _safe(f_blame, {"authors": []})
    commits_stats = _safe(f_stats, [])
    first = _safe(f_first, {})
    open_prs_data = _safe(f_open, {"prs": [], "degraded": True})

    open_pr_authors = {p.get("author", "") for p in open_prs_data.get("prs", [])}

    contribs = expertise.score_contributors(
        blame_authors=blame_data.get("authors", []),
        commits_with_stats=commits_stats,
        first_author=first,
        departed_patterns=git_ops.read_departed(),
        open_pr_authors=open_pr_authors,
        now=datetime.now(timezone.utc),
    )
    for c in contribs:
        c["status"] = employees.status(c.get("name", ""), c.get("email", ""))

    team_slug = owners_match["owners"][0] if owners_match["owners"] else ""
    team_short = codeowners.short_team_name(team_slug) if team_slug else ""
    routing = owners_map.lookup(team_slug) if team_slug else {}

    return {
        "path": path,
        "missing": False,
        "team_slug": team_slug,
        "team_short": team_short or "Unowned",
        "team_inferred": owners_match.get("inferred", False),
        "team_inferred_from": owners_match.get("inferred_from"),
        "routing": routing,
        "contributors": contribs[:5],
        "open_prs": open_prs_data.get("prs", []),
        "open_prs_degraded": open_prs_data.get("degraded", False),
    }


def _safe(future, default: Any) -> Any:
    try:
        return future.result(timeout=10)
    except Exception:
        return default


# ---------- Aggregation ----------

def aggregate_reviewers(files: list[dict], current_team_members: dict[str, set[str]]) -> dict:
    """Sum DOK scores per author across all changed files; flag still-on-team and departed.

    current_team_members maps team_slug -> set of github logins (from gh_teams members).
    We can't perfectly match git author name -> github login, so still_on_team is
    best-effort substring matching against team member logins.
    """
    by_person: dict[str, dict] = {}
    for f in files:
        if f.get("missing"):
            continue
        team_slug = f.get("team_slug", "")
        team_members = current_team_members.get(team_slug, set())
        for c in f.get("contributors", []):
            name = c.get("name", "")
            if not name:
                continue
            key = (c.get("email", "") or name).lower()
            rec = by_person.setdefault(key, {
                "name": name,
                "email": c.get("email", ""),
                "total_score": 0.0,
                "files": [],
                "max_score": 0.0,
                "status": c.get("status", "unknown"),
                "is_departed": c.get("is_departed", False),
                "still_on_team": False,
                "github_login": None,
            })
            rec["total_score"] += max(0.0, c.get("score", 0.0))
            rec["max_score"] = max(rec["max_score"], c.get("score", 0.0))
            rec["files"].append({"path": f["path"], "score": round(c.get("score", 0.0), 2)})
            # Best-effort GH login match: name initials/parts vs team member logins.
            if not rec["github_login"]:
                login = _guess_github_login(name, team_members)
                if login:
                    rec["github_login"] = login
                    rec["still_on_team"] = True

    # Drop departed (negative score after penalty makes them irrelevant for "ping next")
    # but keep them in a separate list so the UI can flag stale CODEOWNERS reviewers.
    active = [r for r in by_person.values() if not r["is_departed"] and r["total_score"] > 0]
    departed = [r for r in by_person.values() if r["is_departed"]]

    active.sort(key=lambda r: (-r["total_score"], -len(r["files"])))
    departed.sort(key=lambda r: -len(r["files"]))

    return {
        "recommended": [_strip(r) for r in active[:5]],
        "departed_with_history": [_strip(r) for r in departed[:5]],
    }


def _strip(r: dict) -> dict:
    return {
        "name": r["name"],
        "email": r["email"],
        "github_login": r.get("github_login"),
        "still_on_team": r.get("still_on_team", False),
        "status": r.get("status", "unknown"),
        "total_score": round(r["total_score"], 2),
        "max_score": round(r["max_score"], 2),
        "files_count": len(r["files"]),
        "files": r["files"][:8],
    }


def _guess_github_login(name: str, team_logins: set[str]) -> Optional[str]:
    """Cheap heuristic: try to match a git author name to a GH login from the team.
    Tries: lowercase last name, first.last, first-letter+last name. Best-effort only.
    """
    if not name or not team_logins:
        return None
    parts = [p.lower() for p in name.split() if p]
    if not parts:
        return None
    candidates = set()
    candidates.update(parts)  # any single name part
    if len(parts) >= 2:
        candidates.add(f"{parts[0]}.{parts[-1]}")
        candidates.add(f"{parts[0]}{parts[-1]}")
        candidates.add(f"{parts[0][0]}{parts[-1]}")
    for cand in candidates:
        for login in team_logins:
            if cand == login.lower() or cand in login.lower():
                return login
    return None


def aggregate_teams(files: list[dict]) -> list[dict]:
    """Group files by their CODEOWNERS team."""
    by_team: dict[str, dict] = {}
    for f in files:
        if f.get("missing"):
            continue
        slug = f.get("team_slug") or "_unowned"
        rec = by_team.setdefault(slug, {
            "team_slug": slug,
            "team_short": f.get("team_short", "Unowned"),
            "files": [],
            "any_inferred": False,
            "routing": f.get("routing", {}),
        })
        rec["files"].append(f["path"])
        if f.get("team_inferred"):
            rec["any_inferred"] = True
    out = list(by_team.values())
    out.sort(key=lambda t: (-len(t["files"]), t["team_short"]))
    for t in out:
        t["files_count"] = len(t["files"])
    return out


def aggregate_conflicts(files: list[dict], own_pr_number: int) -> list[dict]:
    """Find PRs (other than ours) that touch any of the same files."""
    by_pr: dict[int, dict] = {}
    for f in files:
        for pr in f.get("open_prs", []):
            n = pr.get("number")
            if not n or n == own_pr_number:
                continue
            rec = by_pr.setdefault(n, {
                "number": n,
                "title": pr.get("title", ""),
                "author": pr.get("author", ""),
                "url": pr.get("url", ""),
                "files": [],
            })
            rec["files"].append(f["path"])
    out = list(by_pr.values())
    out.sort(key=lambda p: -len(p["files"]))
    for p in out:
        p["files_count"] = len(p["files"])
    return out


def compute_risk(pr: dict, files: list[dict], reviewers: dict, teams: list[dict],
                 conflicts: list[dict]) -> dict:
    """Surface PR-level risk flags for the Risk tab. Best-effort, no false alarms.
    Severity is the count of high-severity flags — UI bins it into low/med/high.
    """
    flags = []
    now = datetime.now(timezone.utc)

    updated = _parse_iso(pr.get("updatedAt"))
    if updated and pr.get("state", "").upper() == "OPEN":
        days = (now - updated).days
        if days >= 7:
            flags.append({"severity": "high", "type": "stale",
                          "copy": f"PR is {days}d old with no recent activity"})
        elif days >= 3:
            flags.append({"severity": "med", "type": "stale",
                          "copy": f"PR is {days}d old; check if reviewers are blocked"})

    # Departed assigned reviewers (CODEOWNERS catches a name no longer at the company).
    departed_reviewers = []
    for rev in (pr.get("reviewRequests") or []):
        login = rev.get("login") or rev.get("name", "")
        if not login:
            continue
        if employees.status(login, "") == "departed":
            departed_reviewers.append(login)
    if departed_reviewers:
        flags.append({"severity": "high", "type": "departed_reviewer",
                      "copy": f"Assigned reviewer(s) departed: {', '.join(departed_reviewers)}"})

    # Reviewers with no expertise on changed code.
    if not reviewers["recommended"]:
        flags.append({"severity": "med", "type": "no_experts",
                      "copy": "No active engineer has prior history with the changed files"})

    if conflicts:
        biggest = conflicts[0]
        flags.append({
            "severity": "med" if biggest["files_count"] < 3 else "high",
            "type": "conflict",
            "copy": f"PR #{biggest['number']} touches {biggest['files_count']} of the same file(s)",
        })

    if len(teams) > 1:
        team_names = ", ".join(t["team_short"] for t in teams)
        flags.append({"severity": "med", "type": "cross_team",
                      "copy": f"Touches {len(teams)} teams: {team_names} — coordinate review"})

    files_count = len([f for f in files if not f.get("missing")])
    additions = pr.get("additions", 0)
    if files_count >= 30 or additions >= 1000:
        flags.append({"severity": "med", "type": "large",
                      "copy": f"Large PR: {files_count} files, +{additions} lines"})

    high = sum(1 for f in flags if f["severity"] == "high")
    med = sum(1 for f in flags if f["severity"] == "med")
    level = "high" if high >= 1 else "med" if med >= 2 else "med" if med == 1 else "low"

    return {"level": level, "flags": flags, "high_count": high, "med_count": med}


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------- Top-level orchestration ----------

def investigate(raw_ident: str) -> dict:
    """Single entry point. Always returns a dict; never raises."""
    parsed = parse_ident(raw_ident)
    if parsed["kind"] == "invalid":
        return {"error": "input must be a PR number, GitHub PR URL, or Jira key (e.g. LPCD-1560)",
                "input": raw_ident}

    if parsed["kind"] == "jira":
        pr_number = resolve_jira_to_pr(parsed["value"])
        if not pr_number:
            return {"error": f"No PR found with title matching {parsed['value']}",
                    "input": raw_ident}
    else:
        pr_number = parsed["value"]

    cache_key = f"pr:{GH_REPO}:{pr_number}"
    cached = _cache_get(cache_key, _PR_CACHE_TTL)

    try:
        pr = fetch_pr(pr_number)
    except Exception as e:
        # If gh failed but we have a cached result, serve it (degraded).
        if cached:
            cached["degraded"] = True
            cached["degraded_reason"] = f"gh refresh failed: {e}"
            return cached
        return {"error": f"gh pr view {pr_number} failed: {e}", "input": raw_ident}

    # Use cache if PR head hasn't moved (no new commits since we last analyzed).
    if cached and cached.get("pr", {}).get("head_sha") == pr.get("headRefOid"):
        return cached

    file_paths = [f["path"] for f in pr.get("files", []) if f.get("path")]
    if not file_paths:
        return {"error": f"PR #{pr_number} has no files", "input": raw_ident, "pr": _pr_summary(pr)}

    # Per-file lite investigation, fanned out.
    workers = min(8, max(2, len(file_paths)))
    file_results: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(investigate_file_lite, p): p for p in file_paths}
        for fut, p in futures.items():
            try:
                file_results.append(fut.result(timeout=30))
            except Exception:
                file_results.append({"path": p, "missing": True, "error": "timeout"})

    # Attach +/- counts from gh's per-file additions/deletions.
    add_del = {f["path"]: (f.get("additions", 0), f.get("deletions", 0)) for f in pr.get("files", [])}
    for fr in file_results:
        a, d = add_del.get(fr["path"], (0, 0))
        fr["additions"] = a
        fr["deletions"] = d

    # Pull team-membership sets once for the reviewer aggregator.
    team_members: dict[str, set[str]] = {}
    for f in file_results:
        slug = f.get("team_slug")
        if not slug or slug in team_members:
            continue
        routing = f.get("routing", {})
        team_members[slug] = {m.get("login", "").lower() for m in (routing.get("members") or [])}

    reviewers = aggregate_reviewers(file_results, team_members)
    teams = aggregate_teams(file_results)
    conflicts = aggregate_conflicts(file_results, pr_number)
    risk = compute_risk(pr, file_results, reviewers, teams, conflicts)

    case = {
        "pr": _pr_summary(pr),
        "files": file_results,
        "files_count": len([f for f in file_results if not f.get("missing")]),
        "missing_files_count": len([f for f in file_results if f.get("missing")]),
        "reviewers": reviewers,
        "teams": teams,
        "conflicts": conflicts,
        "risk": risk,
        "_generated_at": int(time.time()),
    }
    _cache_put(cache_key, case)
    return case


def _pr_summary(pr: dict) -> dict:
    author = (pr.get("author") or {}).get("login", "")
    raw_reviews = [
        {"author": (r.get("author") or {}).get("login", ""),
         "state": r.get("state", ""),
         "submitted_at": r.get("submittedAt", "")}
        for r in (pr.get("reviews") or [])
    ]
    # Build per-author latest verdict (mirrors GitHub's merge-block rule).
    # APPROVED / CHANGES_REQUESTED override earlier COMMENTED entries from the
    # same person; we keep the most recent of those two states.
    latest_by_author: dict[str, dict] = {}
    for rv in sorted(raw_reviews, key=lambda r: r.get("submitted_at", "")):
        a = rv.get("author", "")
        if not a:
            continue
        if rv.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest_by_author[a] = rv
        elif a not in latest_by_author:
            latest_by_author[a] = rv  # COMMENTED counts only if no firmer verdict
    review_summary = list(latest_by_author.values())
    review_summary.sort(key=lambda r: r.get("submitted_at", ""), reverse=True)

    body = (pr.get("body") or "").strip()
    return {
        "number": pr.get("number"),
        "title": pr.get("title", ""),
        "body": body,
        "body_truncated": body[:600] + ("…" if len(body) > 600 else ""),
        "url": pr.get("url", ""),
        "state": pr.get("state", ""),
        "is_draft": pr.get("isDraft", False),
        "author": author,
        "additions": pr.get("additions", 0),
        "deletions": pr.get("deletions", 0),
        "head_branch": pr.get("headRefName", ""),
        "base_branch": pr.get("baseRefName", ""),
        "head_sha": pr.get("headRefOid", ""),
        "created_at": pr.get("createdAt", ""),
        "updated_at": pr.get("updatedAt", ""),
        "merged_at": pr.get("mergedAt"),
        "review_requests": [r.get("login") or r.get("name", "") for r in (pr.get("reviewRequests") or [])],
        "reviews": raw_reviews,
        "review_summary": review_summary,
    }


# ---------- Cache ----------

def _cache_get(key: str, ttl: int):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    f = CACHE_DIR / (hashlib.sha1(key.encode()).hexdigest() + ".json")
    if not f.exists() or time.time() - f.stat().st_mtime > ttl:
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None


def _cache_put(key: str, data: dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    f = CACHE_DIR / (hashlib.sha1(key.encode()).hexdigest() + ".json")
    try:
        f.write_text(json.dumps(data))
    except OSError:
        pass
