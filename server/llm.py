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
    "You are Codemap. You read a file's code, its recent commits, and the Jira "
    "tickets those commits reference. Speak as if the file is explaining its own "
    "existence to a new engineer — what business problem it solves, what trade-offs "
    "have been made, and what's in motion right now. Return strict JSON only.\n\n"
    "PRIMARY OUTPUTS:\n"
    "- purpose: ONE sentence on the business reason this file exists. Lead with "
    "  the problem being solved, not 'This file contains X'. Ground in file_head "
    "  identifiers + jira_tickets[].description + commit bodies. If there's no "
    "  business signal at all (no Jira tickets, terse commits, empty comments), "
    "  return an empty string — do NOT pad.\n"
    "- decisions: Array of {claim, evidence} objects (0-4 items). Each claim is "
    "  ONE short sentence describing a business or design decision visible in the "
    "  evidence. Evidence is a short verbatim quote (<=120 chars) from a Jira "
    "  ticket, commit subject/body, or code comment — plus its source label "
    "  like 'LPCD-1719', 'commit abc1234', or 'comment'. If there's nothing worth "
    "  saying, return [] — do NOT fabricate decisions to fill space.\n"
    "- gotchas: Up to 3 short bullets on non-obvious behavior or constraints "
    "  (found in TODO/FIXME/NOTE/HACK/WARNING comments, rollback notes, or Jira "
    "  caveats). Empty array if nothing is notable.\n\n"
    "FORBIDDEN PHRASES (rewrite if you find yourself typing these):\n"
    "- 'This file contains...'  -> just state the purpose directly.\n"
    "- 'Recent changes include...'  -> name the decision, cite the evidence.\n"
    "- 'It includes tests for...'  -> say what invariant the tests protect and why.\n"
    "- 'various improvements', 'general updates', 'enhancements to...' (filler).\n"
    "- ANY paragraph that reads like a Wikipedia summary. This is engineer-to-"
    "  engineer, not executive-to-board.\n\n"
    "EVIDENCE RULES (strict):\n"
    "- Every claim in decisions[] must cite evidence that exists VERBATIM in the "
    "  input signals (jira_tickets, commits, file_head). Do not paraphrase into "
    "  the evidence field — quote.\n"
    "- Prefer jira_tickets[].description as evidence when available — that's "
    "  where the business 'why' lives. Fall back to commit bodies, then subjects.\n"
    "- Never invent Jira IDs, PR numbers, SHAs, function names, or URLs. If "
    "  unsure, omit.\n\n"
    "SECONDARY OUTPUTS (terse, for other UI sections):\n"
    "- summary_copy: 1 sentence, format '<team_short> owns this. <1 clause on activity>.'\n"
    "- activity_summary: 1 sentence on cadence (<=22 words).\n"
    "- timeline_notes: one plain-English sentence per commit in commits[], same order.\n"
    "- why: 0-3 bullets about routing. Empty array if nothing specific.\n"
    "- next_step: optional; only if there's an obvious action grounded in signals.\n\n"
    "OWNERSHIP:\n"
    "- If ownership_inferred is true, mention 'inferred from parent dir' once.\n"
    "- If ownership_inferred is false, do NOT mention inference."
)

SCHEMA_HINT = {
    "purpose": "ONE sentence on the business reason this file exists, or empty string if no business signal.",
    "decisions": [
        {
            "claim": "One short sentence naming a business or design decision.",
            "evidence": "Short verbatim quote from a Jira ticket / commit / comment, plus its source label e.g. 'LPCD-1719'.",
        }
    ],
    "gotchas": ["Up to 3 short bullets. Empty if nothing notable."],
    "summary_copy": "1 sentence on owner + headline activity.",
    "activity_summary": "1 sentence on merge/open cadence.",
    "timeline_notes": ["One sentence per commit in commits[], same order."],
    "why": ["Optional bullets; [] is fine."],
    "next_step": {
        "title": "short imperative",
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
        "purpose": "",
        "decisions": [],
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
    if isinstance(data.get("purpose"), str) and data["purpose"].strip():
        out["purpose"] = data["purpose"].strip()
    if isinstance(data.get("decisions"), list):
        clean_decisions: list[dict] = []
        for item in data["decisions"]:
            if not isinstance(item, dict):
                continue
            claim = str(item.get("claim") or "").strip()
            evidence = str(item.get("evidence") or "").strip()
            if claim and evidence:
                clean_decisions.append({"claim": claim[:280], "evidence": evidence[:280]})
        out["decisions"] = clean_decisions[:4]
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
