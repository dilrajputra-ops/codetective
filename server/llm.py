"""Local-only LLM via Ollama. No code or org metadata leaves the machine.

Falls back to a templated narrative if Ollama is unreachable, slow, or returns garbage.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import CACHE_DIR, OLLAMA_HOST, OLLAMA_MODEL

# LLM responses are deterministic for a given signal payload (we use temp=0.2
# but the same input -> the same output 99% of the time). Cache for an hour;
# git/PR data changes will naturally bust the key by changing signal contents.
LLM_CACHE_DIR = CACHE_DIR / "llm"
LLM_CACHE_TTL_SECONDS = 3600

SYSTEM = (
    "You are Codemap, a code-context tool. Given structured signals about a file path "
    "in the gobroker monorepo, produce SHORT narrative for an engineer who needs to "
    "know who owns it and what to do next. Return strict JSON only.\n\n"
    "Rules:\n"
    "- NEVER say the file is 'Unowned' as if that's the answer. If owners is empty, "
    "  describe the actual recent activity using top_contributors and commit subjects.\n"
    "- If ownership_inferred is true, say 'likely owned by X (inferred from parent dir)'.\n"
    "- Ground every claim in a signal. No filler.\n"
    "- If routing.slack_primary is set, mention it as the place to ask.\n"
    "- Prefer top_contributors with still_on_team=true when suggesting who to ping; "
    "  warn if the largest blame share has status='departed' or still_on_team=false "
    "  (someone who left the team or company).\n"
    "- Use merged_prs_30d_count and merged_prs_90d_count to characterize churn: "
    "  '0 open / 0 merged 90d' = stale; '0 open / 4 merged 30d' = quiet right now but actively maintained.\n"
    "- For timeline_notes: write ONE short plain-English sentence per commit in commits[], "
    "  in the SAME ORDER. Describe the engineering intent, not just rephrasing the subject. "
    "  Reference Jira IDs when present in the subject. Skip the author name."
)

SCHEMA_HINT = {
    "summary_copy": "1-2 sentences on recent activity in this area, grounded in the signals.",
    "activity_summary": "1 sentence (<= 22 words) on what's happening in this area lately: themes across recent commits, open PRs, and merge cadence. Plain English.",
    "timeline_notes": [
        "Array, one entry per commit in commits[] in the same order. Each is a short plain-English sentence (<= 18 words) describing what that commit did and why."
    ],
    "why": ["3 short bullets explaining why this team is the right first stop."],
    "next_step": {
        "title": "short imperative title (e.g. 'Open PR #123 first')",
        "copy": "1 sentence on why",
        "link_label": "e.g. 'Open pull request'",
        "url": "best URL to start with",
    },
}


def _fallback(signals: dict) -> dict:
    team = signals.get("team_short", "this area")
    owners = signals.get("owners") or []
    inferred = signals.get("ownership_inferred", False)
    inferred_from = signals.get("ownership_inferred_from")
    n_commits = len(signals.get("commits", []))
    open_prs = signals.get("open_prs", [])
    top = signals.get("top_contributors", [])
    commits = signals.get("commits", [])
    path = signals.get("path", "this file")

    # Summary that doesn't lean on "Unowned"
    if owners and not inferred:
        summary = f"Owned by {team}. {n_commits} recent commits scanned."
    elif inferred:
        summary = f"No direct CODEOWNERS rule. Nearest parent ({inferred_from}) routes to {team}."
    elif top:
        names = ", ".join(t["name"] for t in top[:2])
        last_subject = (commits[0]["subject"][:80] if commits else "").strip()
        summary = (
            f"No CODEOWNERS rule. Most recent activity by {names}"
            + (f"; latest: \"{last_subject}\"." if last_subject else ".")
        )
    else:
        summary = f"No CODEOWNERS rule and no recent commits found for {path}."

    why = []
    if owners and not inferred:
        why.append(f"CODEOWNERS routes this path directly to {team}.")
    elif inferred:
        why.append(f"No direct rule; nearest parent dir ({inferred_from}) is owned by {team}.")
    if top:
        why.append(f"Top blame: {', '.join(t['name'] for t in top[:3])}.")
    m30 = signals.get("merged_prs_30d_count", 0)
    m90 = signals.get("merged_prs_90d_count", 0)
    if open_prs:
        why.append(f"{len(open_prs)} open PR(s) touch this path right now.")
    elif m30 > 0:
        why.append(f"Quiet right now but actively maintained: {m30} PR(s) merged in last 30d.")
    elif m90 > 0:
        why.append(f"No recent activity but {m90} PR(s) merged in last 90d.")
    elif n_commits == 0:
        why.append("No commits or PRs found in scanned window — file may be effectively dead.")
    if not why:
        why = ["No ownership or recent-activity signals found.", "Treat as orphan code; ask in #eng-broker."]

    next_url = ""
    next_title = "Read recent history"
    next_copy = "No active PR; start with the most recent merged commit."
    if open_prs:
        pr = open_prs[0]
        next_url = pr.get("url", "")
        next_title = f"Open PR #{pr.get('number')} first"
        next_copy = "Active branch on this exact path; read it before older history."
    elif commits:
        c = commits[0]
        next_title = "Read latest commit"
        next_copy = c.get("subject", "")[:120]

    # Activity summary fallback: lean on metric counts.
    open_n = len(open_prs)
    m30 = signals.get("merged_prs_30d_count", 0)
    m90 = signals.get("merged_prs_90d_count", 0)
    if open_n:
        activity_summary = f"{open_n} open PR{'s' if open_n != 1 else ''} in flight; {m30} merged in last 30d, {m90} in last 90d."
    elif m30:
        activity_summary = f"No open PRs right now, but {m30} merged in the last 30 days — actively maintained."
    elif m90:
        activity_summary = f"Quiet area: nothing open, only {m90} merged in the last 90 days."
    elif n_commits:
        activity_summary = f"No recent PR activity; last {n_commits} commits scanned for context."
    else:
        activity_summary = "No recent commits or PRs found for this path."

    # timeline_notes fallback: trimmed subject for each commit, parallel-indexed.
    timeline_notes = [
        (c.get("subject") or "").strip()[:140] or "(no subject)"
        for c in commits[:8]
    ]

    return {
        "summary_copy": summary,
        "activity_summary": activity_summary,
        "timeline_notes": timeline_notes,
        "why": why,
        "next_step": {
            "title": next_title,
            "copy": next_copy,
            "link_label": "Open link",
            "url": next_url,
        },
    }


def _ollama_chat(messages: list[dict], timeout: float = 8.0) -> str | None:
    body = json.dumps(
        {
            "model": OLLAMA_MODEL,
            "messages": messages,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2, "num_predict": 400},
        }
    ).encode()
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return (data.get("message") or {}).get("content")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def _cache_key(signals: dict) -> str:
    canonical = json.dumps(signals, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def _cache_read(key: str) -> dict | None:
    f = LLM_CACHE_DIR / f"{key}.json"
    if not f.exists():
        return None
    try:
        if time.time() - f.stat().st_mtime > LLM_CACHE_TTL_SECONDS:
            return None
        return json.loads(f.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _cache_write(key: str, data: dict) -> None:
    try:
        LLM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (LLM_CACHE_DIR / f"{key}.json").write_text(json.dumps(data))
    except OSError:
        pass


def synthesize(signals: dict) -> dict:
    key = _cache_key(signals)
    cached = _cache_read(key)
    if cached is not None:
        return cached

    user = json.dumps({"signals": signals, "output_schema": SCHEMA_HINT}, default=str)[:8000]
    raw = _ollama_chat(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        timeout=8.0,
    )
    if not raw:
        out = _fallback(signals)
        _cache_write(key, out)
        return out
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        out = _fallback(signals)
        _cache_write(key, out)
        return out

    out = _fallback(signals)
    if isinstance(data.get("summary_copy"), str) and data["summary_copy"].strip():
        out["summary_copy"] = data["summary_copy"].strip()
    if isinstance(data.get("activity_summary"), str) and data["activity_summary"].strip():
        out["activity_summary"] = data["activity_summary"].strip()
    if isinstance(data.get("timeline_notes"), list) and data["timeline_notes"]:
        notes = [str(x).strip() for x in data["timeline_notes"] if str(x).strip()]
        if notes:
            # Pad with fallback if model returned fewer than expected so indexing is safe.
            n_expected = len(out["timeline_notes"]) or len(notes)
            if len(notes) < n_expected:
                notes = notes + out["timeline_notes"][len(notes):n_expected]
            out["timeline_notes"] = notes[:max(n_expected, len(notes))]
    if isinstance(data.get("why"), list) and data["why"]:
        out["why"] = [str(x).strip() for x in data["why"] if str(x).strip()][:5] or out["why"]
    if isinstance(data.get("next_step"), dict):
        ns = data["next_step"]
        for k in ("title", "copy", "link_label", "url"):
            if isinstance(ns.get(k), str) and ns[k].strip():
                out["next_step"][k] = ns[k].strip()
    _cache_write(key, out)
    return out
