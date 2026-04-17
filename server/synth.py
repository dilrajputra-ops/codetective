"""Combine signals from git/codeowners/gh into the CASE-shaped dict the UI expects."""
from __future__ import annotations

import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import hashlib

PR_NUMBER_RE = re.compile(r"\(#(\d+)\)\s*$")


def _pr_number(subject: str) -> Optional[int]:
    """Extract trailing `(#NNNN)` PR number from a squash-merge commit subject."""
    m = PR_NUMBER_RE.search(subject or "")
    return int(m.group(1)) if m else None


def _jira_links_in(text: str) -> list[dict]:
    """Find Jira IDs (deduped, in order) and return [{id, url}, …]."""
    seen: dict[str, dict] = {}
    for m in jira_extract.JIRA_RE.finditer(text or ""):
        jid = m.group(1)
        if jid not in seen:
            seen[jid] = {"id": jid, "url": jira_extract.JIRA_BASE + jid}
    return list(seen.values())

from . import (
    codeowners,
    employees,
    expertise,
    gh_client,
    git_ops,
    jira_client,
    jira_extract,
    llm,
    owners_map,
    vectors,
)


def fingerprint(path: str) -> str:
    """Cheap content fingerprint for short-circuiting unchanged investigations.
    Inputs that change rarely: codeowners rule + last commit sha touching path.
    Excludes open-PR signal (would require a slow gh call to compute)."""
    try:
        co = codeowners.match_with_inference(path)
        co_part = f"{co.get('source')}:{co.get('line')}:{','.join(co.get('owners') or [])}"
    except Exception:
        co_part = ""
    sha = git_ops.latest_sha(path) or ""
    return hashlib.sha1(f"{co_part}|{sha}".encode()).hexdigest()[:12]


def _safe(future, default: Any, label: str) -> Any:
    """Resolve a future, swallowing exceptions and logging to stderr.
    Lets one slow/broken signal degrade gracefully without 500-ing the whole call.
    """
    try:
        return future.result(timeout=15)
    except Exception as e:
        print(f"[synth] {label} failed: {e!r}", file=sys.stderr)
        return default

PALETTE = ["#6366f1", "#2563eb", "#7c3aed", "#0891b2", "#059669", "#db2777"]


def _initials(name: str) -> str:
    parts = [p for p in name.split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _ago(iso: str) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        days = delta.days
        if days <= 0:
            hours = max(1, delta.seconds // 3600)
            return f"{hours} h ago"
        if days < 30:
            return f"{days} d ago"
        if days < 365:
            return f"{days // 30} mo ago"
        return f"{days // 365} y ago"
    except Exception:
        return iso[:10]


def _short_date(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%b %d")
    except Exception:
        return iso[:10]


def _commits_in_30d(commits: list[dict]) -> int:
    cutoff = datetime.now(timezone.utc).timestamp() - 30 * 86400
    n = 0
    for c in commits:
        try:
            ts = datetime.fromisoformat(c["date"].replace("Z", "+00:00")).timestamp()
            if ts >= cutoff:
                n += 1
        except Exception:
            pass
    return n


def _confidence(owners: list[str], commits_30d: int) -> str:
    if owners and commits_30d >= 2:
        return "High"
    if owners or commits_30d >= 1:
        return "Med"
    return "Low"


def _build_contributors(
    blame_authors,
    commits_with_stats,
    first_author_data,
    open_pr_authors,
    routing,
    still_on_team_fn,
):
    """Score + format contributor list. Pulled out of investigate so we can
    rebuild it cheaply in stage 3 once open-PR authors are known."""
    scored = expertise.score_contributors(
        blame_authors=blame_authors,
        commits_with_stats=commits_with_stats,
        first_author=first_author_data,
        departed_patterns=git_ops.read_departed(),
        open_pr_authors=open_pr_authors,
        now=datetime.now(timezone.utc),
    )
    contributors = []
    for i, c in enumerate(scored[:5]):
        name = c["name"]
        email = c.get("email", "")
        emp_status = employees.status(name, email)
        on_team = still_on_team_fn(name, email)
        if emp_status == "departed":
            role = "Left Alpaca"
        elif on_team:
            role = f"Current {routing.get('team_name', 'team')} member"
        else:
            role = c["role"]
        key = email or name
        contributors.append(
            {
                "name": name,
                "email": email,
                "initials": _initials(name),
                "color": PALETTE[i % len(PALETTE)],
                "role": role,
                "when": _ago(c["last_active"]),
                "lines": c["lines"],
                "score": round(c["score"], 2),
                "score_breakdown": c["score_breakdown"],
                "is_departed": c.get("is_departed", False) or emp_status == "departed",
                "status": emp_status,
                "still_on_team": on_team,
            }
        )
    return scored, contributors


def _context_codeowners(owners_match):
    if owners_match.get("source") and owners_match.get("line"):
        copy = (
            f"{owners_match['source']}:{owners_match['line']} → "
            f"{', '.join(owners_match['owners']) or '(none)'}"
        )
        if owners_match.get("inferred"):
            copy += f"  (inferred from parent: {owners_match['inferred_from']})"
        return {"title": "CODEOWNERS", "copy": copy}
    return {"title": "CODEOWNERS", "copy": "No direct or parent-directory CODEOWNERS rule matches this path."}


def investigate_stream(path: str, range_str: Optional[str]):
    """Yield staged partial case dicts as data resolves.

    Stage order maps onto latency classes:
      1. shell        — codeowners + similar + routing       (~200ms, instant local + cached gh-teams)
      2. contributors — blame + log + scoring + timeline      (~1s, local subprocess)
      3. github       — open/merged PRs + jira + evidence     (1-5s, network gh)
      4. narrative    — LLM summary/why/next-step             (3-10s, ollama)

    Each yielded dict carries a `stage` key plus whatever case fields
    became available at that stage. Consumer is expected to deep-merge.
    """
    if not git_ops.file_exists(path):
        raise FileNotFoundError(f"Path not found in gobroker: {path}")

    start = end = None
    if range_str:
        try:
            a, b = range_str.replace(" ", "").split("-", 1)
            start, end = int(a), int(b)
        except Exception:
            start = end = None

    open_pr = None
    latest_commit = None
    team_short = "Unowned"
    contributors_scored: list = []
    contributors: list = []
    open_prs: list = []
    merged_30 = {"prs": [], "count": 0, "days": 30, "degraded": True, "error": None}
    merged_90 = {"prs": [], "count": 0, "days": 90, "degraded": True, "error": None}
    jira_ids: list = []
    owners_match: dict = {"owners": [], "rule": None, "line": None, "source": None, "inferred": False, "inferred_from": None}
    commits: list = []
    blame: list = []
    commits_stats: list = []
    first_author_data: dict = {}
    routing: dict = {}

    with ThreadPoolExecutor(max_workers=10) as ex:
        f_blame = ex.submit(git_ops.blame, path, start, end)
        f_log = ex.submit(git_ops.log, path, 20)
        f_owners_match = ex.submit(codeowners.match_with_inference, path)
        f_open = ex.submit(gh_client.open_prs_touching, path)
        f_merged_30 = ex.submit(gh_client.merged_prs_touching, path, 30)
        f_merged_90 = ex.submit(gh_client.merged_prs_touching, path, 90)
        f_stats = ex.submit(git_ops.commits_with_stats, path, 50)
        f_first = ex.submit(git_ops.first_author, path)
        f_similar = ex.submit(vectors.similar, path, 5)

        # ============ Stage shell: codeowners + similar + routing ============
        owners_match = _safe(f_owners_match, owners_match, "codeowners")
        similar = _safe(f_similar, [], "vector_similar")
        routing = (
            owners_map.lookup(owners_match["owners"][0])
            if owners_match["owners"]
            else {}
        )
        shell_title = None
        if owners_match["owners"]:
            shell_title = codeowners.short_team_name(owners_match["owners"][0])
            if owners_match.get("inferred"):
                shell_title = f"{shell_title} (inferred)"

        yield {
            "stage": "shell",
            "path": path,
            "range": range_str or "",
            "fingerprint": fingerprint(path),
            "similar": similar,
            "routing": routing,
            "summary": {"title": shell_title} if shell_title else {},
            "team": {
                "lane": owners_match["owners"][0] if owners_match["owners"] else "",
                "inferred": owners_match.get("inferred", False),
                "inferred_from": owners_match.get("inferred_from"),
            },
            "sources": {
                "codeowners": (
                    f"{owners_match['source']}:{owners_match['line']}"
                    if owners_match["source"]
                    else None
                ),
                "vector_index": vectors.index_size(),
            },
        }

        # ============ Stage contributors: blame + scoring + timeline ============
        blame_data = _safe(f_blame, {"authors": [], "lines_by_author": {}}, "blame")
        blame = blame_data["authors"]
        commits = _safe(f_log, [], "log")
        commits_stats = _safe(f_stats, [], "commits_with_stats")
        first_author_data = _safe(f_first, {}, "first_author")

        commits_30d = _commits_in_30d(commits)
        if owners_match["owners"]:
            team_short = codeowners.short_team_name(owners_match["owners"][0])
            if owners_match.get("inferred"):
                team_short = f"{team_short} (inferred)"
        elif blame:
            team_short = blame[0]["name"].split()[0] + "'s area"
        else:
            team_short = "Unowned"

        if owners_match.get("inferred"):
            confidence = "Med" if commits_30d >= 1 else "Low"
        else:
            confidence = _confidence(owners_match["owners"], commits_30d)

        team_member_names = {
            (m.get("login") or "").lower() for m in (routing.get("members") or [])
        }

        def _still_on_team(name: str, email: str) -> bool:
            if not team_member_names:
                return False
            n = (name or "").lower().replace(" ", "")
            if any(login and (login in n or n in login) for login in team_member_names):
                return True
            em = (email or "").lower()
            if "+" in em and "@users.noreply.github.com" in em:
                login = em.split("+", 1)[1].split("@", 1)[0]
                if login in team_member_names:
                    return True
            return False

        # First pass: no PR-author bonus (open PRs not yet fetched). Re-runs in stage 3.
        contributors_scored, contributors = _build_contributors(
            blame, commits_stats, first_author_data, set(), routing, _still_on_team
        )

        timeline_commits = commits[:5]
        timeline = []
        for c in timeline_commits:
            pr_num = _pr_number(c["subject"])
            sha7 = c["sha"][:7]
            timeline.append(
                {
                    "date": _short_date(c["date"]),
                    "title": c["subject"][:140],
                    "author": c["author"],
                    "sha": sha7,
                    "when": _ago(c["date"]),
                    "pr_number": pr_num,
                    "jira": _jira_links_in(c["subject"]),
                    "url": (
                        f"https://github.com/alpacahq/gobroker/pull/{pr_num}"
                        if pr_num
                        else f"https://github.com/alpacahq/gobroker/commit/{c['sha']}"
                    ),
                    "copy": f"{c['author']} · {sha7}",
                }
            )

        latest_commit = commits[0] if commits else None
        latest_pr = (
            {
                "badge": "Latest commit",
                "id": latest_commit["sha"][:7],
                "title": latest_commit["subject"][:120],
                "meta": f"{latest_commit['author']} · {_ago(latest_commit['date'])}",
                "url": f"https://github.com/alpacahq/gobroker/commit/{latest_commit['sha']}",
            }
            if latest_commit
            else None
        )

        yield {
            "stage": "contributors",
            "summary": {"title": team_short},
            "team": {
                "lane": (
                    owners_match["owners"][0]
                    if owners_match["owners"]
                    else ("via blame" if blame else "Unowned")
                ),
                "confidence": confidence,
                "activity": f"{commits_30d} commits in 30 d",
                "inferred": owners_match.get("inferred", False),
                "inferred_from": owners_match.get("inferred_from"),
            },
            "contributors": contributors,
            "timeline": timeline,
            "latestPr": latest_pr,
            "context": [
                _context_codeowners(owners_match),
                {
                    "title": "Activity window",
                    "copy": f"{commits_30d} commits in last 30 days, {len(commits)} scanned overall.",
                },
            ],
            "sources": {"commits_scanned": len(commits)},
        }

        # ============ Stage github: open/merged PRs + jira + evidence ============
        pr_data = _safe(f_open, {"prs": [], "degraded": True, "error": "fetch failed"}, "open_prs")
        open_prs = pr_data["prs"]
        merged_30 = _safe(f_merged_30, merged_30, "merged_30")
        merged_90 = _safe(f_merged_90, merged_90, "merged_90")

        # Jira IDs from PR titles/branches + commit subjects/bodies. Bodies require N subprocess calls.
        jira_sources = []
        for pr in open_prs:
            jira_sources.append({"text": pr.get("title", ""), "where": f"PR #{pr.get('number')} title"})
            jira_sources.append({"text": pr.get("branch", ""), "where": f"PR #{pr.get('number')} branch"})
        for c in commits[:10]:
            jira_sources.append({"text": c.get("subject", ""), "where": f"commit {c['sha'][:7]}"})
        commit_bodies: dict[str, str] = {}
        if commits:
            with ThreadPoolExecutor(max_workers=10) as ex2:
                body_futs = {c["sha"]: ex2.submit(git_ops.commit_body, c["sha"]) for c in commits[:10]}
            for sha, fut in body_futs.items():
                body = _safe(fut, "", f"commit_body {sha[:7]}")
                if body:
                    commit_bodies[sha] = body
                    jira_sources.append({"text": body, "where": f"commit {sha[:7]} body"})
        jira_ids = jira_extract.extract(jira_sources)

        # Kick off Jira ticket-body fetches NOW so they run in parallel with
        # the LLM file_head read + prompt assembly (saves ~3s cold). Resolved
        # later in the narrative stage. Daemon-style: never blocks if acli hangs.
        f_jira_tickets = ex.submit(jira_client.fetch_many, [j["id"] for j in jira_ids[:3]])

        open_pr = open_prs[0] if open_prs else None

        # Re-rank contributors with the open-PR-author signal layered in.
        if open_prs:
            open_pr_authors = {pr["author"] for pr in open_prs if pr.get("author")}
            contributors_scored, contributors = _build_contributors(
                blame, commits_stats, first_author_data, open_pr_authors, routing, _still_on_team
            )

        github_payload = {
            "stage": "github",
            "openPr": (
                {
                    "badge": "Open PR",
                    "id": f"#{open_pr['number']}",
                    "title": open_pr["title"],
                    "meta": f"{open_pr['author']} · updated {_ago(open_pr['updated_at'])}",
                    "url": open_pr["url"],
                }
                if open_pr
                else None
            ),
            "jira": (
                {
                    "badge": "Jira",
                    "id": jira_ids[0]["id"],
                    "title": f"Mentioned in {jira_ids[0]['where']}",
                    "meta": "Linked from recent activity",
                    "url": jira_ids[0]["url"],
                }
                if jira_ids
                else None
            ),
            "mergedPrs": {
                "30d": {
                    "count": merged_30["count"],
                    "degraded": merged_30["degraded"],
                    "prs": [
                        {
                            "badge": "Merged",
                            "id": f"#{p['number']}",
                            "title": p["title"][:120],
                            "meta": f"{p['author']} · merged {_ago(p['updated_at'])}",
                            "url": p["url"],
                        }
                        for p in merged_30["prs"][:5]
                    ],
                },
                "90d": {
                    "count": merged_90["count"],
                    "degraded": merged_90["degraded"],
                    "prs": [
                        {
                            "badge": "Merged",
                            "id": f"#{p['number']}",
                            "title": p["title"][:120],
                            "meta": f"{p['author']} · merged {_ago(p['updated_at'])}",
                            "url": p["url"],
                        }
                        for p in merged_90["prs"][:5]
                    ],
                },
            },
            "evidence": (
                [
                    {"title": f"PR #{pr['number']}", "copy": pr["title"][:120], "label": "open pr", "url": pr["url"]}
                    for pr in open_prs[:3]
                ]
                + [
                    {"title": f"Jira {j['id']}", "copy": f"Found in {j['where']}", "label": "jira", "url": j["url"]}
                    for j in jira_ids[:2]
                ]
            ),
            "team": {
                "activity": f"{commits_30d} commits in 30 d · {len(open_prs)} open PR{'s' if len(open_prs) != 1 else ''}",
            },
            "context": [
                _context_codeowners(owners_match),
                {
                    "title": "Activity window",
                    "copy": f"{commits_30d} commits in last 30 days, {len(commits)} scanned overall.",
                },
                {
                    "title": "Open work",
                    "copy": (
                        f"{len(open_prs)} open · {merged_30['count']} merged in 30d · {merged_90['count']} merged in 90d."
                        if not pr_data["degraded"]
                        else f"GitHub data unavailable ({pr_data['error']})."
                    ),
                },
            ],
            "sources": {
                "open_prs": len(open_prs),
                "gh_degraded": pr_data["degraded"],
            },
        }
        if open_prs:
            github_payload["contributors"] = contributors

        yield github_payload

    # ============ Stage narrative: LLM call (slowest, runs after executor closes) ============
    # File head: package decl, imports, doc comments, top-level types/functions.
    # This is what gives the LLM something concrete to explain — without it, the
    # narrative is all metadata and zero code-context.
    file_head_text = ""
    try:
        fh = git_ops.read_file(path, max_bytes=12_000, max_lines=80)
        file_head_text = "\n".join(fh.get("lines") or [])[:6000]
    except Exception:
        pass

    # Pick up the Jira fetches kicked off back in the github stage. By now
    # they're usually done (parallel with PR fan-out + file_head read).
    try:
        jira_tickets = f_jira_tickets.result(timeout=10)
    except Exception:
        jira_tickets = []

    llm_signals = {
        "path": path,
        "range": range_str or "",
        "team_short": team_short,
        "owners": owners_match["owners"],
        "owners_rule": owners_match["rule"],
        "ownership_inferred": owners_match.get("inferred", False),
        "ownership_inferred_from": owners_match.get("inferred_from"),
        "file_head": file_head_text,
        "commits": [
            {
                "date": c["date"],
                "subject": c["subject"],
                "author": c["author"],
                "body": (commit_bodies.get(c["sha"], "") or "")[:600],
            }
            for c in commits[:5]
        ],
        "open_prs": open_prs[:3],
        "merged_prs_30d": [{"number": p["number"], "title": p["title"], "author": p["author"], "merged_at": p["updated_at"]} for p in merged_30["prs"][:3]],
        "merged_prs_30d_count": merged_30["count"],
        "merged_prs_90d_count": merged_90["count"],
        "jira": jira_ids[:3],
        "jira_tickets": [
            {
                "id": t["id"],
                "summary": t.get("summary", ""),
                "description": (t.get("description") or "")[:800],
                "issuetype": t.get("issuetype", ""),
                "status": t.get("status", ""),
            }
            for t in jira_tickets if not t.get("error")
        ],
        "top_contributors": [
            {
                "name": c["name"],
                "lines": c["lines"],
                "status": contributors[i]["status"] if i < len(contributors) else "unknown",
                "still_on_team": contributors[i]["still_on_team"] if i < len(contributors) else False,
            }
            for i, c in enumerate(contributors_scored[:5])
        ],
        "current_team_members": [
            (m.get("login") or "") for m in (routing.get("members") or [])
        ][:20],
        "routing": {
            "team_name": routing.get("team_name"),
            "slack_primary": (routing.get("slack") or {}).get("primary"),
            "on_call": routing.get("on_call"),
            "escalation": routing.get("escalation"),
        },
    }
    narrative = llm.synthesize(llm_signals)

    yield {
        "stage": "narrative",
        "summary": {"title": team_short, "copy": narrative["summary_copy"]},
        "purpose": narrative.get("purpose", ""),
        "decisions": narrative.get("decisions", []),
        "gotchas": narrative.get("gotchas", []),
        "activitySummary": narrative.get("activity_summary", ""),
        "timelineNotes": narrative.get("timeline_notes", []),
        "why": narrative["why"],
        "nextStep": {
            "title": narrative["next_step"]["title"],
            "copy": narrative["next_step"]["copy"],
            "linkLabel": narrative["next_step"]["link_label"] + " →",
            "url": narrative["next_step"]["url"]
            or (
                open_pr["url"]
                if open_pr
                else (latest_commit and f"https://github.com/alpacahq/gobroker/commit/{latest_commit['sha']}")
                or ""
            ),
        },
        "model": narrative.get("model", ""),
        "sources": {"llm_used": "(fallback)" not in (narrative.get("model") or "")},
    }


def _merge_partial(case: dict, partial: dict) -> None:
    """Shallow-merge a partial stage payload into the running case dict.
    Nested dicts get .update()'d (so e.g. summary={"title":...} then summary={"copy":...}
    yields summary={title,copy}). Lists/scalars get replaced."""
    for k, v in partial.items():
        if k == "stage":
            continue
        if isinstance(v, dict) and isinstance(case.get(k), dict):
            case[k].update(v)
        else:
            case[k] = v


def investigate(path: str, range_str: Optional[str]) -> dict:
    """Synchronous flat-result variant. Drains the streaming generator and merges.
    Kept for backward compatibility (smoke test, CLI callers, fallback path)."""
    case: dict = {}
    for partial in investigate_stream(path, range_str):
        _merge_partial(case, partial)
    return case
