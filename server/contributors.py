"""Org-wide contributor aggregation for the /contributors page.

Combines three data sources:
  - gh_roster: full alpacahq GitHub roster (login + display name)
  - gh_teams:  per-team rosters we've cached during investigations
  - git log:   authorship counts + top files + recent commits in gobroker

Expensive ops (git shortlog across full history, per-author file stats) are
cached on disk with long TTLs. The list view is designed to be served from
a single `git shortlog -sne` scan (~2s for gobroker) + in-memory roster joins.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from collections import Counter
from pathlib import Path

from . import gh_roster, gh_teams, git_ops
from .config import CACHE_DIR, GOBROKER_PATH

SHORTLOG_CACHE = CACHE_DIR / "contrib-shortlog.json"
# Commit counts drift slowly; daily refresh is plenty and keeps the page snappy.
SHORTLOG_TTL = 24 * 60 * 60
DETAIL_CACHE_DIR = CACHE_DIR / "contrib-detail"
# Detail view (top files + recent commits) changes faster; refresh every 6h.
DETAIL_TTL = 6 * 60 * 60

_SHORTLOG_RE = re.compile(r"^\s*(\d+)\s+(.+?)\s+<(.+?)>\s*$")

# Catch-all GitHub teams that include nearly everyone — filtered from
# per-person pills so cards aren't dominated by noise. They stay in the cache.
_NOISY_TEAM_SLUGS = {"engineering", "read-only-members", "members"}


# ---------- git shortlog: per-author commit counts across the whole repo ----------

def _run_shortlog() -> list[dict]:
    """`git shortlog -sne HEAD`: <count> <name> <email>, sorted desc.
    Returns list of {name, email, commits}. ~2s on gobroker."""
    try:
        out = subprocess.run(
            ["git", "shortlog", "-sne", "HEAD"],
            cwd=str(GOBROKER_PATH),
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return []
        rows: list[dict] = []
        for line in out.stdout.splitlines():
            m = _SHORTLOG_RE.match(line)
            if not m:
                continue
            rows.append({
                "commits": int(m.group(1)),
                "name": m.group(2).strip(),
                "email": m.group(3).strip().lower(),
            })
        return rows
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


def _last_commit_per_email() -> dict[str, str]:
    """Most recent author date per email across all of HEAD.

    Single shellout, ~2s on gobroker. `git log` outputs commits in reverse
    chronological order, so the first occurrence per email is the most
    recent author timestamp. Returned as ISO strings."""
    try:
        out = subprocess.run(
            ["git", "log", "HEAD", "--format=%ae%x09%aI", "--no-merges"],
            cwd=str(GOBROKER_PATH),
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return {}
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {}
    seen: dict[str, str] = {}
    for line in out.stdout.splitlines():
        if "\t" not in line:
            continue
        email, date = line.split("\t", 1)
        email = email.strip().lower()
        if email and email not in seen:
            seen[email] = date.strip()
    return seen


def _load_shortlog() -> list[dict]:
    """Returns shortlog rows enriched with `last_commit` ISO date per email."""
    if SHORTLOG_CACHE.exists():
        try:
            data = json.loads(SHORTLOG_CACHE.read_text())
            if time.time() - data.get("_fetched_at", 0) < SHORTLOG_TTL:
                return data.get("rows") or []
        except (OSError, json.JSONDecodeError):
            pass
    rows = _run_shortlog()
    last_dates = _last_commit_per_email()
    for r in rows:
        r["last_commit"] = last_dates.get(r["email"], "")
    try:
        SHORTLOG_CACHE.parent.mkdir(parents=True, exist_ok=True)
        SHORTLOG_CACHE.write_text(json.dumps({"_fetched_at": int(time.time()), "rows": rows}))
    except OSError:
        pass
    return rows


# ---------- login resolution: email -> github login using the roster ----------

_NOREPLY_RE = re.compile(r"^(?:\d+\+)?([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))@users\.noreply\.github\.com$")


def _normalize(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _email_to_login(name: str, email: str) -> str:
    """Reuses same resolution rules as synth._github_login but in one place."""
    em = (email or "").strip().lower()
    if em:
        m = _NOREPLY_RE.match(em)
        if m:
            return m.group(1)
    # Fall back to the org-wide roster (handles name + email-prefix matches).
    return gh_roster.find_login(name=name, email=email)


# ---------- list view: join shortlog with roster + teams ----------

def list_all() -> dict:
    """Return the contributor index for the list page.

    Joins:
      - Every alpacahq GitHub roster member (217 people) — source of truth for
        "who is on the team right now" (includes people with 0 gobroker commits).
      - Git shortlog rows — author commit counts. Some rows won't match a roster
        entry (contractors, personal emails, former employees who are gone).

    Output: a flat list keyed primarily by login when known, otherwise by the
    git author identity. Sorted by commit count desc.
    """
    roster_map = gh_roster._memo  # type: ignore[attr-defined]
    if roster_map is None:
        gh_roster.find_login(name="", email="")  # force load
        roster_map = gh_roster._memo  # type: ignore[attr-defined]
    roster_members = (roster_map or {}).get("members") or []
    login_to_name = {m["login"]: m.get("name") or "" for m in roster_members}

    shortlog = _load_shortlog()

    # Accumulate per-login (so one engineer with multiple git identities merges).
    by_login: dict[str, dict] = {}
    unmatched: list[dict] = []
    for row in shortlog:
        login = _email_to_login(row["name"], row["email"])
        last = row.get("last_commit") or ""
        if not login:
            # Still surface them so the page is honest about unresolved authors.
            unmatched.append({
                "login": "",
                "name": row["name"],
                "email": row["email"],
                "commits": row["commits"],
                "last_active": last,
                "resolved": False,
            })
            continue
        entry = by_login.get(login)
        if entry is None:
            entry = {
                "login": login,
                "name": login_to_name.get(login) or row["name"],
                "commits": 0,
                "emails": [],
                "last_active": "",
                "resolved": True,
            }
            by_login[login] = entry
        entry["commits"] += row["commits"]
        if row["email"] not in entry["emails"]:
            entry["emails"].append(row["email"])
        # Keep the most recent of all the engineer's git identities.
        if last and (not entry["last_active"] or last > entry["last_active"]):
            entry["last_active"] = last

    # Include roster members who haven't committed (new hires, non-engineers).
    for m in roster_members:
        login = m["login"]
        if login not in by_login:
            by_login[login] = {
                "login": login,
                "name": m.get("name") or "",
                "commits": 0,
                "emails": [],
                "last_active": "",
                "resolved": True,
            }

    # Enrich with team memberships from the bulk team cache. `gh_teams.refresh_all`
    # populates this on startup so every engineer can be tagged with their team(s),
    # not just teams referenced by recent investigations. Catch-all teams (e.g.
    # `engineering`, which contains all 149 engineers) are filtered out so the
    # per-card pills carry actual signal.
    team_cache = gh_teams._load_cache()  # type: ignore[attr-defined]
    login_teams: dict[str, list[dict]] = {}
    teams_summary: list[dict] = []
    for slug, entry in (team_cache or {}).items():
        team_name = entry.get("name") or slug
        members = entry.get("members") or []
        teams_summary.append({
            "slug": slug,
            "name": team_name,
            "members_count": entry.get("members_count") or len(members),
            "noisy": slug in _NOISY_TEAM_SLUGS,
        })
        if slug in _NOISY_TEAM_SLUGS:
            continue
        for member in members:
            login = member.get("login")
            if not login:
                continue
            login_teams.setdefault(login, []).append({"slug": slug, "name": team_name})

    for entry in by_login.values():
        entry_teams = login_teams.get(entry["login"], [])
        entry["teams"] = sorted({t["name"] for t in entry_teams})
        entry["team_slugs"] = sorted({t["slug"] for t in entry_teams})

    resolved = sorted(by_login.values(), key=lambda e: (-e["commits"], e["login"].lower()))
    unmatched.sort(key=lambda e: -e["commits"])

    # Order team chips by how many resolved committers belong to each team —
    # most-relevant team first so the chip bar is immediately useful.
    team_committer_count: Counter = Counter()
    for entry in resolved:
        if entry["commits"] <= 0:
            continue
        for t in entry["team_slugs"]:
            team_committer_count[t] += 1
    teams_summary = [t for t in teams_summary if not t["noisy"]]
    teams_summary.sort(key=lambda t: (-team_committer_count.get(t["slug"], 0), t["name"]))
    for t in teams_summary:
        t["committer_count"] = team_committer_count.get(t["slug"], 0)

    return {
        "total": len(resolved) + len(unmatched),
        "resolved_count": sum(1 for e in resolved if e["commits"] > 0),
        "unresolved_count": len(unmatched),
        "teams": teams_summary,
        "contributors": resolved,
        "unmatched": unmatched[:20],  # cap — not actionable beyond the top few
    }


# ---------- detail view: top files + recent commits for one login ----------

def _detail_cache_path(login: str) -> Path:
    return DETAIL_CACHE_DIR / f"{login.lower()}.json"


def _load_detail_cache(login: str) -> dict | None:
    p = _detail_cache_path(login)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        if time.time() - data.get("_fetched_at", 0) < DETAIL_TTL:
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _save_detail_cache(login: str, data: dict) -> None:
    try:
        DETAIL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _detail_cache_path(login).write_text(json.dumps(data))
    except OSError:
        pass


def _emails_for_login(login: str) -> list[str]:
    """Reverse the shortlog to find which emails belong to this login.
    Handles the case where one engineer has both work + noreply emails."""
    rows = _load_shortlog()
    result: list[str] = []
    for row in rows:
        if _email_to_login(row["name"], row["email"]).lower() == login.lower():
            result.append(row["email"])
    return result


def _git_log_author(author_pattern: str, limit: int = 50) -> list[dict]:
    """`git log --author=<regex> --name-only` → list of commits with files.

    Important: git's --author uses BRE by default, where `|` is a literal
    character. We pass --extended-regexp so alternation across multiple
    emails (`a@b|c@d`) works. Without this, multi-email engineers silently
    return zero commits."""
    fmt = "%H%x09%ad%x09%an%x09%ae%x09%s"
    try:
        out = subprocess.run(
            ["git", "log",
             "--extended-regexp",
             f"--author={author_pattern}",
             "--all",
             "--regexp-ignore-case",
             f"-n{limit}",
             "--name-only",
             f"--pretty=format:{fmt}",
             "--date=iso-strict"],
            cwd=str(GOBROKER_PATH),
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    # Output alternates: header line, then file lines, blank, repeat.
    commits: list[dict] = []
    current: dict | None = None
    for line in out.stdout.splitlines():
        if "\t" in line and line.count("\t") >= 4:
            # New commit header.
            if current is not None:
                commits.append(current)
            sha, date, name, email, subject = line.split("\t", 4)
            current = {
                "sha": sha, "date": date, "author": name, "email": email,
                "subject": subject, "files": [],
            }
        elif line.strip() and current is not None:
            current["files"].append(line.strip())
    if current is not None:
        commits.append(current)
    return commits


def detail(login: str) -> dict:
    """Return detail payload for one contributor's profile page."""
    login = (login or "").strip()
    if not login:
        return {"error": "login is required"}

    cached = _load_detail_cache(login)
    if cached:
        return cached

    # Roster lookup — if not a known org member we still generate a minimal view
    # so investigators can still see git activity for contractors / departed eng.
    roster = gh_roster._memo or {}  # type: ignore[attr-defined]
    roster_members = roster.get("members") or []
    roster_entry = next((m for m in roster_members if m["login"].lower() == login.lower()), None)

    emails = _emails_for_login(login)
    if not emails:
        # User committed under noreply email we can't find in shortlog (rare).
        # Fallback to searching by name from roster if available.
        if roster_entry and roster_entry.get("name"):
            emails = [roster_entry["name"]]

    author_pattern = "|".join(re.escape(e) for e in emails) if emails else login
    commits = _git_log_author(author_pattern, limit=80)

    # Aggregate: top files by touch count, commits in 30d / 90d, first/last commit.
    file_touches: Counter = Counter()
    for c in commits:
        for f in c["files"]:
            file_touches[f] += 1

    now = int(time.time())
    def _days_ago(iso_date: str) -> int:
        try:
            return int((now - int(time.mktime(time.strptime(iso_date[:10], "%Y-%m-%d")))) / 86400)
        except Exception:
            return 99999

    commits_30d = sum(1 for c in commits if _days_ago(c["date"]) <= 30)
    commits_90d = sum(1 for c in commits if _days_ago(c["date"]) <= 90)
    total_commits = sum(r["commits"] for r in _load_shortlog() if r["email"] in emails)

    # Derive a team list from gh_teams cache.
    team_cache = gh_teams._load_cache() or {}  # type: ignore[attr-defined]
    teams = []
    for slug, entry in team_cache.items():
        for m in entry.get("members") or []:
            if (m.get("login") or "").lower() == login.lower():
                teams.append({
                    "slug": slug,
                    "name": entry.get("name") or slug,
                    "html_url": entry.get("html_url"),
                })
                break

    payload = {
        "login": login,
        "name": (roster_entry or {}).get("name") or (commits[0]["author"] if commits else login),
        "github_url": f"https://github.com/{login}",
        "in_org": roster_entry is not None,
        "emails": emails,
        "stats": {
            "total_commits": total_commits or len(commits),
            "commits_30d": commits_30d,
            "commits_90d": commits_90d,
            "first_commit": commits[-1]["date"] if commits else "",
            "last_commit": commits[0]["date"] if commits else "",
        },
        "teams": sorted(teams, key=lambda t: t["name"]),
        "top_files": [
            {"path": p, "commits": n}
            for p, n in file_touches.most_common(15)
        ],
        "recent_commits": [
            {
                "sha": c["sha"][:7],
                "sha_full": c["sha"],
                "date": c["date"][:10],
                "subject": c["subject"][:140],
                "files_touched": len(c["files"]),
                "url": f"https://github.com/alpacahq/gobroker/commit/{c['sha']}",
            }
            for c in commits[:20]
        ],
        "_fetched_at": now,
    }
    _save_detail_cache(login, payload)
    return payload
