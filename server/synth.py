"""Combine signals from git/codeowners/gh into the CASE-shaped dict the UI expects."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import codeowners, employees, gh_client, git_ops, jira_extract, llm, owners_map, vectors

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


def investigate(path: str, range_str: Optional[str]) -> dict:
    if not git_ops.file_exists(path):
        raise FileNotFoundError(f"Path not found in gobroker: {path}")

    start = end = None
    if range_str:
        try:
            a, b = range_str.replace(" ", "").split("-", 1)
            start, end = int(a), int(b)
        except Exception:
            start = end = None

    blame_data = git_ops.blame(path, start, end)
    blame = blame_data["authors"]
    blame_lines = blame_data["lines_by_author"]
    commits = git_ops.log(path, limit=20)
    owners_match = codeowners.match_with_inference(path)
    pr_data = gh_client.open_prs_touching(path)
    open_prs = pr_data["prs"]

    # Jira IDs from PR titles, branches, last 10 commit subjects + bodies
    jira_sources = []
    for pr in open_prs:
        jira_sources.append({"text": pr.get("title", ""), "where": f"PR #{pr.get('number')} title"})
        jira_sources.append({"text": pr.get("branch", ""), "where": f"PR #{pr.get('number')} branch"})
    for c in commits[:10]:
        jira_sources.append({"text": c.get("subject", ""), "where": f"commit {c['sha'][:7]}"})
        body = git_ops.commit_body(c["sha"])
        if body:
            jira_sources.append({"text": body, "where": f"commit {c['sha'][:7]} body"})
    jira_ids = jira_extract.extract(jira_sources)

    commits_30d = _commits_in_30d(commits)
    if owners_match["owners"]:
        team_short = codeowners.short_team_name(owners_match["owners"][0])
        if owners_match.get("inferred"):
            team_short = f"{team_short} (inferred)"
    elif blame:
        team_short = blame[0]["name"].split()[0] + "'s area"
    else:
        team_short = "Unowned"
    # Inferred ownership = at most Med confidence
    if owners_match.get("inferred"):
        confidence = "Med" if commits_30d >= 1 else "Low"
    else:
        confidence = _confidence(owners_match["owners"], commits_30d)

    # Routing info for the owning team (auto-fetched + manual overrides).
    routing = (
        owners_map.lookup(owners_match["owners"][0])
        if owners_match["owners"]
        else {}
    )
    # Build "still on the owning team?" lookup. Match by full name; GH login isn't in blame.
    team_member_names = {
        (m.get("login") or "").lower() for m in (routing.get("members") or [])
    }
    # Github usernames don't always match git author names, so we also check the
    # noreply email pattern: 12345+login@users.noreply.github.com
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

    # Contributors: top 5 by blame lines, with deterministic role tags + status.
    open_pr_authors = {pr["author"] for pr in open_prs if pr.get("author")}
    top_blame = blame[:5]
    contributors = []
    for i, b in enumerate(top_blame):
        name = b["name"]
        email = b.get("email", "")
        emp_status = employees.status(name, email)
        on_team = _still_on_team(name, email)
        if emp_status == "departed":
            role = "Left Alpaca"
        elif on_team:
            role = f"Current {routing.get('team_name','team')} member"
        elif name in open_pr_authors:
            role = "Open PR owner"
        elif i == 0:
            role = "Largest blame share"
        else:
            role = "Recent committer"
        key = email or name
        contributors.append(
            {
                "name": name,
                "email": email,
                "initials": _initials(name),
                "color": PALETTE[i % len(PALETTE)],
                "role": role,
                "when": _ago(b["last_date"]),
                "lines": b["lines"],
                "snippets": blame_lines.get(key, [])[:5],
                "status": emp_status,           # active | departed | unknown
                "still_on_team": on_team,
            }
        )

    timeline = [
        {
            "date": _short_date(c["date"]),
            "title": c["subject"][:90],
            "copy": f"{c['author']} · {c['sha'][:7]}",
        }
        for c in commits[:5]
    ]

    open_pr = open_prs[0] if open_prs else None
    latest_commit = commits[0] if commits else None

    # LLM narrative
    llm_signals = {
        "path": path,
        "range": range_str or "",
        "team_short": team_short,
        "owners": owners_match["owners"],
        "owners_rule": owners_match["rule"],
        "ownership_inferred": owners_match.get("inferred", False),
        "ownership_inferred_from": owners_match.get("inferred_from"),
        "commits": [{"date": c["date"], "subject": c["subject"], "author": c["author"]} for c in commits[:8]],
        "open_prs": open_prs[:3],
        "jira": jira_ids[:3],
        "top_contributors": [
            {
                "name": b["name"],
                "lines": b["lines"],
                "status": contributors[i]["status"],
                "still_on_team": contributors[i]["still_on_team"],
            }
            for i, b in enumerate(top_blame)
        ],
        "current_team_members": [
            (m.get("login") or "")
            for m in (routing.get("members") or [])
        ][:20],
        "routing": {
            "team_name": routing.get("team_name"),
            "slack_primary": (routing.get("slack") or {}).get("primary"),
            "on_call": routing.get("on_call"),
            "escalation": routing.get("escalation"),
        },
    }
    narrative = llm.synthesize(llm_signals)

    # Similar paths (best-effort; empty if vector index isn't built yet).
    try:
        similar = vectors.similar(path, k=5)
    except Exception:
        similar = []

    case = {
        "summary": {"title": team_short, "copy": narrative["summary_copy"]},
        "team": {
            "lane": (
                owners_match["owners"][0]
                if owners_match["owners"]
                else ("via blame" if blame else "Unowned")
            ),
            "activity": f"{commits_30d} commits in 30 d · {len(open_prs)} open PR{'s' if len(open_prs) != 1 else ''}",
            "confidence": confidence,
            "inferred": owners_match.get("inferred", False),
            "inferred_from": owners_match.get("inferred_from"),
        },
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
        "latestPr": (
            {
                "badge": "Latest commit",
                "id": latest_commit["sha"][:7],
                "title": latest_commit["subject"][:120],
                "meta": f"{latest_commit['author']} · {_ago(latest_commit['date'])}",
                "url": f"https://github.com/alpacahq/gobroker/commit/{latest_commit['sha']}",
            }
            if latest_commit
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
        "contributors": contributors,
        "routing": routing,
        "timeline": timeline,
        "why": narrative["why"],
        "context": [
            {
                "title": "CODEOWNERS",
                "copy": (
                    (
                        f"{owners_match['source']}:{owners_match['line']} → "
                        f"{', '.join(owners_match['owners']) or '(none)'}"
                        + (
                            f"  (inferred from parent: {owners_match['inferred_from']})"
                            if owners_match.get("inferred")
                            else ""
                        )
                    )
                    if owners_match.get("source") and owners_match.get("line")
                    else "No direct or parent-directory CODEOWNERS rule matches this path."
                ),
            },
            {
                "title": "Activity window",
                "copy": f"{commits_30d} commits in last 30 days, {len(commits)} scanned overall.",
            },
            {
                "title": "Open work",
                "copy": (
                    f"{len(open_prs)} open PR{'s' if len(open_prs) != 1 else ''} touching this path."
                    if not pr_data["degraded"]
                    else f"GitHub data unavailable ({pr_data['error']})."
                ),
            },
        ],
        "evidence": [
            {
                "title": f"PR #{pr['number']}",
                "copy": pr["title"][:120],
                "label": "open pr",
                "url": pr["url"],
            }
            for pr in open_prs[:3]
        ]
        + [
            {
                "title": f"Jira {j['id']}",
                "copy": f"Found in {j['where']}",
                "label": "jira",
                "url": j["url"],
            }
            for j in jira_ids[:2]
        ],
        "nextStep": {
            "title": narrative["next_step"]["title"],
            "copy": narrative["next_step"]["copy"],
            "linkLabel": narrative["next_step"]["link_label"] + " →",
            "url": narrative["next_step"]["url"]
            or (open_pr["url"] if open_pr else (latest_commit and f"https://github.com/alpacahq/gobroker/commit/{latest_commit['sha']}")
            or ""),
        },
        "similar": similar,
        "sources": {
            "codeowners": f"{owners_match['source']}:{owners_match['line']}" if owners_match["source"] else None,
            "commits_scanned": len(commits),
            "open_prs": len(open_prs),
            "gh_degraded": pr_data["degraded"],
            "llm_used": bool(narrative.get("summary_copy")),
            "vector_index": vectors.index_size(),
        },
        "path": path,
        "range": range_str or "",
    }
    return case
