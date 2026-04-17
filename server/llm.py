"""Local-only LLM via Ollama. No code or org metadata leaves the machine.

Falls back to a templated narrative if Ollama is unreachable, slow, or returns garbage.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .config import OLLAMA_HOST, OLLAMA_MODEL

SYSTEM = (
    "You are Codemap, a code-context tool. Given structured signals about a file path "
    "in the gobroker monorepo, produce a SHORT narrative for an engineer who needs to "
    "know who owns it and what to do next. Return strict JSON only.\n\n"
    "Rules:\n"
    "- NEVER say the file is 'Unowned' as if that's the answer. If owners is empty, "
    "  describe the actual recent activity using top_contributors and commit subjects.\n"
    "- If ownership_inferred is true, say 'likely owned by X (inferred from parent dir)'.\n"
    "- Ground every claim in a signal. No filler.\n"
    "- If routing.slack_primary is set, mention it as the place to ask.\n"
    "- Prefer top_contributors with still_on_team=true when suggesting who to ping; "
    "  warn if the largest blame share has status='departed' or still_on_team=false "
    "  (someone who left the team or company)."
)

SCHEMA_HINT = {
    "summary_copy": "1-2 sentences on recent activity in this area, grounded in the signals.",
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
    if open_prs:
        why.append(f"{len(open_prs)} open PR(s) touch this path right now.")
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

    return {
        "summary_copy": summary,
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


def synthesize(signals: dict) -> dict:
    user = json.dumps({"signals": signals, "output_schema": SCHEMA_HINT}, default=str)[:8000]
    raw = _ollama_chat(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        timeout=8.0,
    )
    if not raw:
        return _fallback(signals)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _fallback(signals)

    out = _fallback(signals)
    if isinstance(data.get("summary_copy"), str) and data["summary_copy"].strip():
        out["summary_copy"] = data["summary_copy"].strip()
    if isinstance(data.get("why"), list) and data["why"]:
        out["why"] = [str(x).strip() for x in data["why"] if str(x).strip()][:5] or out["why"]
    if isinstance(data.get("next_step"), dict):
        ns = data["next_step"]
        for k in ("title", "copy", "link_label", "url"):
            if isinstance(ns.get(k), str) and ns[k].strip():
                out["next_step"][k] = ns[k].strip()
    return out
