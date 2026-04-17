"""LLM-generated engineer profile summary for the /contributors detail page.

Reuses the existing local-only Ollama runtime (`server.llm`) and produces a
short, evidence-grounded narrative about what an engineer works on, derived
purely from gobroker signals (top files, recent commit subjects, teams,
activity recency). No data leaves the machine.

Cached on disk for 24h keyed by signal fingerprint, so refreshing a profile
within the cache window is instant.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import CACHE_DIR, OLLAMA_HOST, OLLAMA_MODEL

SUMMARY_CACHE_DIR = CACHE_DIR / "contrib-summary"
SUMMARY_TTL = 24 * 60 * 60

# Hard cap how much we send to the LLM. Each contributor signal is small
# (paths + commit subjects + team names) so 6KB is plenty for high-quality
# context without paying for huge prompt tokens.
_MAX_PROMPT_BYTES = 6000


SYSTEM = (
    "You read an engineer's gobroker activity (top files, recent commits, "
    "teams, tenure) and write a 2-3 sentence profile so a teammate can tell "
    "at a glance what this person works on. Return strict JSON only.\n\n"
    "WRITE LIKE AN ENGINEER, NOT AN HR BIO:\n"
    "- Lead with the concrete domain they touch (cash interest, paper trading, "
    "  reg-T margin, HSA, options assignment, ledger reconciliation, ACH, etc.).\n"
    "- Name the layer (REST controllers, OMS, ledger models, db migrations, "
    "  background workers, broker entities, etc.) — derive this from file paths.\n"
    "- One sentence on what they're shipping right now if recent_commits has "
    "  enough signal; skip if dormant.\n\n"
    "FORBIDDEN PHRASES:\n"
    "- 'demonstrates strong focus on', 'showcasing expertise', 'keen eye for'.\n"
    "- 'this contributor', 'their work highlights', 'their activities suggest'.\n"
    "- 'a deep understanding of', 'particular emphasis on', 'in their ecosystem'.\n"
    "- ANY paragraph that reads like a LinkedIn endorsement.\n\n"
    "EVIDENCE RULES:\n"
    "- Ground every claim in the input. If they have 16 touches on "
    "  rest/api/binder/binder.go, say 'works on the account-binding REST flow' "
    "  not 'shows interest in API design'.\n"
    "- Use the engineer's first name (parsed from `name`) when natural.\n"
    "- If signals are sparse (departed engineer, dormant, <10 commits), say so "
    "  briefly. Do not pad.\n\n"
    "OUTPUTS:\n"
    "- summary: 2-3 sentence paragraph. Plain prose, no bullets, no markdown.\n"
    "- focus_areas: 3-6 short tags (1-3 words each), domain-specific. e.g. "
    "  'cash interest', 'paper trading', 'REST controllers', 'ledger txns'. "
    "  Empty array if signals are too sparse.\n"
    "- recent_themes: 0-3 single-line bullets summarizing what they've been "
    "  shipping recently (last 30-90 days). Each cites a Jira ID or commit "
    "  subject verbatim. Empty if dormant or sparse."
)

SCHEMA_HINT = {
    "summary": "2-3 sentence engineer-to-engineer paragraph. Plain prose.",
    "focus_areas": ["short domain tag", "another tag"],
    "recent_themes": [
        "One-line bullet citing a Jira ID or commit subject verbatim."
    ],
}


def _empty_payload(reason: str = "") -> dict:
    return {
        "summary": "",
        "focus_areas": [],
        "recent_themes": [],
        "model": "",
        "llm_used": False,
        "reason": reason,
    }


def _build_signals(detail: dict) -> dict:
    """Project a contributor detail dict down to the minimum LLM input.
    Keeping this tight avoids both prompt bloat and prompt leakage of fields
    the model shouldn't speculate on (e.g. emails)."""
    s = detail.get("stats") or {}
    return {
        "name": detail.get("name") or detail.get("login"),
        "login": detail.get("login"),
        "in_org": bool(detail.get("in_org")),
        "teams": [t.get("name") for t in (detail.get("teams") or []) if t.get("name")],
        "stats": {
            "total_commits": s.get("total_commits", 0),
            "commits_30d": s.get("commits_30d", 0),
            "commits_90d": s.get("commits_90d", 0),
            "first_commit": (s.get("first_commit") or "")[:10],
            "last_commit": (s.get("last_commit") or "")[:10],
        },
        "top_files": [
            {"path": f.get("path"), "commits": f.get("commits", 0)}
            for f in (detail.get("top_files") or [])[:12]
        ],
        # Subjects are the highest-signal field — Jira IDs + business verbs
        # live here. Strip dates / shas to keep prompt tight; the model only
        # needs the subject text to derive themes.
        "recent_commit_subjects": [
            c.get("subject") for c in (detail.get("recent_commits") or [])[:20]
            if c.get("subject")
        ],
    }


def _signal_fingerprint(signals: dict) -> str:
    """Stable hash over signals so cache busts when activity changes but
    refresh-clicks don't cause re-generation."""
    canonical = json.dumps(signals, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:24]


def _cache_path(login: str, fp: str) -> Path:
    return SUMMARY_CACHE_DIR / f"{login.lower()}_{fp}.json"


def _cache_read(login: str, fp: str) -> dict | None:
    p = _cache_path(login, fp)
    if not p.exists():
        return None
    try:
        if time.time() - p.stat().st_mtime > SUMMARY_TTL:
            return None
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _cache_write(login: str, fp: str, data: dict) -> None:
    try:
        SUMMARY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(login, fp).write_text(json.dumps(data))
    except OSError:
        pass


def _ollama_chat(messages: list[dict], timeout: int = 60) -> str | None:
    """Same options as server.llm._ollama_chat — stays consistent so the
    warmed model isn't forced to reload due to option mismatch."""
    body = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "format": "json",
        "keep_alive": "30m",
        "options": {
            "temperature": 0.3,
            "num_predict": 500,
            "num_ctx": 8192,
            "num_gpu": 999,
        },
    }).encode()
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


def generate(detail: dict) -> dict:
    """Generate (or load cached) engineer profile summary."""
    login = detail.get("login") or ""
    if not login:
        return _empty_payload("missing login")

    signals = _build_signals(detail)

    # Sparse-signal short-circuit: don't bother the LLM if there's nothing
    # to summarize. Returning a shaped empty payload lets the UI render a
    # clean "no profile yet" state instead of garbage prose.
    if not signals["top_files"] and not signals["recent_commit_subjects"]:
        return _empty_payload("no recent activity to summarize")

    fp = _signal_fingerprint(signals)
    cached = _cache_read(login, fp)
    if cached is not None:
        return cached

    user = json.dumps(
        {"signals": signals, "output_schema": SCHEMA_HINT},
        default=str,
    )[:_MAX_PROMPT_BYTES]

    raw = _ollama_chat(
        [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user},
        ],
    )
    if not raw:
        # LLM offline / unreachable. Don't cache the empty so a later
        # request retries once Ollama is back.
        return _empty_payload("local LLM unreachable")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _empty_payload("LLM returned invalid JSON")

    summary = str(data.get("summary") or "").strip()
    focus_areas = data.get("focus_areas") or []
    recent_themes = data.get("recent_themes") or []

    if isinstance(focus_areas, list):
        focus_areas = [str(t).strip()[:40] for t in focus_areas if str(t).strip()][:6]
    else:
        focus_areas = []
    if isinstance(recent_themes, list):
        recent_themes = [str(t).strip()[:200] for t in recent_themes if str(t).strip()][:3]
    else:
        recent_themes = []

    payload = {
        "summary": summary[:800],
        "focus_areas": focus_areas,
        "recent_themes": recent_themes,
        "model": OLLAMA_MODEL,
        "llm_used": bool(summary),
        "reason": "" if summary else "model returned empty summary",
    }
    _cache_write(login, fp, payload)
    return payload
