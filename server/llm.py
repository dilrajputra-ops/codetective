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
    "You are Codemap. Given a file path's code head and recent commits, write a "
    "SHORT engineer-to-engineer briefing on what the file does and what changed lately. "
    "Return strict JSON only.\n\n"
    "PRIMARY OUTPUTS — these are what an engineer actually wants:\n"
    "- code_purpose: What this file does, derived from `file_head` (package decl, doc comments, "
    "  type/struct names, function signatures). Be CONCRETE about identifiers — name the actual "
    "  types/functions defined. If file_head is empty or non-code (yaml/json), describe what the "
    "  config controls based on its keys.\n"
    "- recent_context: What's been happening lately, derived from commit subjects + bodies. "
    "  Group themes ('error handling cleanup', 'TWAP/VWAP support', 'feature-flag rollout'); "
    "  don't just rephrase one commit. Reference Jira IDs from subjects when present.\n"
    "- gotchas: Up to 3 short bullets surfacing non-obvious behavior or constraints found in "
    "  code comments (TODO/FIXME/NOTE/HACK/WARNING) or commit bodies (rollback notes, gotchas, "
    "  follow-ups). Empty array if nothing notable — do NOT pad with generic advice.\n\n"
    "ANTI-HALLUCINATION (strict):\n"
    "- NEVER invent type names, function names, PR numbers, SHAs, URLs, or identifiers. "
    "  Only reference symbols literally present in file_head or signals.\n"
    "- If file_head is empty, say 'No source preview available' for code_purpose — do NOT guess.\n"
    "- next_step.url MUST be copied verbatim from open_prs[].url, latest_commit url, "
    "  merged_prs_30d[].url, or jira[].url. Never assemble URLs.\n"
    "- If open_prs is empty, pick a different next_step — do NOT invent a PR.\n\n"
    "SECONDARY OUTPUTS (terse, optional):\n"
    "- summary_copy: 1 sentence: '<team_short> owns this. <one-clause about activity>.'\n"
    "- activity_summary: 1 sentence (<=22 words) on cadence — open vs merged counts.\n"
    "- timeline_notes: one plain-English sentence per commit in commits[], same order, "
    "  describing engineering intent (skip author names).\n"
    "- why: up to 3 bullets if there's something genuinely useful to say about routing; "
    "  otherwise leave the array empty.\n"
    "- next_step: only if there's an obvious one (open PR to review, etc.); skip otherwise.\n\n"
    "OWNERSHIP:\n"
    "- If ownership_inferred is true, mention 'inferred from parent dir' once.\n"
    "- If ownership_inferred is false, do NOT mention inference — CODEOWNERS is direct."
)

SCHEMA_HINT = {
    "code_purpose": "2-3 sentences on what this file does and why it exists, grounded in file_head identifiers and doc comments.",
    "recent_context": "1-2 sentences on themes across recent commits — what changed, why, any in-flight work.",
    "gotchas": ["Up to 3 short bullets on non-obvious behavior found in comments or commit bodies. Empty if none."],
    "summary_copy": "1 sentence on owner + headline activity.",
    "activity_summary": "1 sentence on merge/open cadence.",
    "timeline_notes": ["One sentence per commit in commits[], same order."],
    "why": ["Optional: bullets about why this team owns the area. Empty array is fine."],
    "next_step": {
        "title": "short imperative — 'Read latest commit', 'Review open PR', 'Ping #eng-payments'. No placeholder numbers.",
        "copy": "1 sentence on why, grounded in a signal.",
        "link_label": "short label",
        "url": "URL copied verbatim from signals; empty string if none fits.",
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

    # Code-context fields are LLM-only. Without an active local LLM, we deliberately
    # leave these empty rather than fake it with raw signal dumps — the UI shows an
    # explicit "no local LLM" empty state instead.
    return {
        "code_purpose": "",
        "recent_context": "",
        "gotchas": [],
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


def _ollama_chat(messages: list[dict], timeout: float = 90.0) -> str | None:
    # 90s headroom: 7B-class models can take ~30-40s cold with the larger code-context
    # prompt (file_head + commit bodies). Warm calls finish in ~10-15s; cache hides repeats.
    body = json.dumps(
        {
            "model": OLLAMA_MODEL,
            "messages": messages,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2, "num_predict": 600},
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
    )
    if not raw:
        out = _fallback(signals)
        out["model"] = f"{OLLAMA_MODEL} (fallback)"
        _cache_write(key, out)
        return out
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        out = _fallback(signals)
        out["model"] = f"{OLLAMA_MODEL} (fallback)"
        _cache_write(key, out)
        return out

    out = _fallback(signals)
    if isinstance(data.get("code_purpose"), str) and data["code_purpose"].strip():
        out["code_purpose"] = data["code_purpose"].strip()
    if isinstance(data.get("recent_context"), str) and data["recent_context"].strip():
        out["recent_context"] = data["recent_context"].strip()
    if isinstance(data.get("gotchas"), list):
        gotchas = [str(x).strip() for x in data["gotchas"] if str(x).strip()][:3]
        out["gotchas"] = gotchas
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
    if isinstance(data.get("why"), list):
        out["why"] = [str(x).strip() for x in data["why"] if str(x).strip()][:5]
    if isinstance(data.get("next_step"), dict):
        ns = data["next_step"]
        for k in ("title", "copy", "link_label", "url"):
            if isinstance(ns.get(k), str) and ns[k].strip():
                out["next_step"][k] = ns[k].strip()
    out["model"] = OLLAMA_MODEL
    _cache_write(key, out)
    return out
