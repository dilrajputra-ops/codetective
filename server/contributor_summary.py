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
    "You write a short, factual engineer-profile blurb from the gobroker "
    "signals provided. Return strict JSON only.\n\n"
    "STRUCTURE (mandatory — fill these EXACT templates, no reordering):\n"
    "- Sentence 1 template:\n"
    "    '{first_name} works on {primary_focus} on the {teams[0]} team.'\n"
    "    If teams is empty, drop the ' on the X team' suffix.\n"
    "    {primary_focus} MUST be `signals.primary_focus` verbatim — never "
    "    a team name, never invented.\n"
    "- Sentence 2 template:\n"
    "    'Also touches {secondary_focuses[0]} and {secondary_focuses[1]}.'\n"
    "    Use 1 secondary if only 1 exists. Skip Sentence 2 if "
    "    secondary_focuses is empty. The phrases come from "
    "    `signals.secondary_focuses` ONLY — never substitute team names.\n"
    "- Sentence 3 (ONLY include if `signals.stats.commits_90d >= 2` AND "
    "  `signals.recent_commit_subjects` is non-empty): 'Recent work is in "
    "  <PREFIX>' where <PREFIX> is the Jira prefix (the letters before the "
    "  first dash) of an actual subject in `recent_commit_subjects`. If "
    "  multiple prefixes appear, use the most common one. If no commit "
    "  subject has a Jira prefix, OMIT this sentence entirely.\n\n"
    "ABSOLUTE RULES (the model has been seen breaking these — do not):\n"
    "- The verb in Sentence 1 is 'works on'. NEVER 'leads', NEVER 'manages', "
    "  NEVER 'maintains'. We have no signal for leadership.\n"
    "- Sentence 2 talks about CODE AREAS only (the secondary_focuses list). "
    "  Team names are NOT code areas. Do not write 'Also touches the X team'.\n"
    "- DO NOT use 'account-binding REST flow' unless that exact string "
    "  appears in `signals.primary_focus`.\n"
    "- DO NOT invent any domain not present in primary_focus or "
    "  secondary_focuses.\n"
    "- DO NOT invent a Jira prefix not visible in recent_commit_subjects.\n"
    "- DO NOT use: 'demonstrates strong focus on', 'showcasing expertise', "
    "  'keen eye for', 'this contributor', 'a deep understanding of', "
    "  'particular emphasis on'.\n\n"
    "SPARSE-SIGNAL HANDLING:\n"
    "- If `stats.total_commits` < 10: write '<First name> is new to gobroker "
    "  — N commits so far, mostly in <primary_focus>.' and stop.\n"
    "- If `stats.last_commit` is more than 1 year ago: lead with '<First "
    "  name> is a past contributor; shipped <primary_focus> through <YYYY>.' "
    "  and skip Sentence 3.\n\n"
    "OUTPUTS:\n"
    "- summary: 2-3 sentence paragraph. Plain prose, no bullets, no markdown.\n"
    "- focus_areas: copy `signals.primary_focus` plus up to 4 entries from "
    "  `signals.secondary_focuses`, deduped. Each <=4 words. Empty if both "
    "  inputs are empty.\n"
    "- recent_themes: 0-3 bullets, each VERBATIM from "
    "  `signals.recent_commit_subjects`. Empty if dormant."
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


# Paths the model should not pattern-match on:
#   - protobuf-generated bindings (*.pb.go, *_pb.go)
#   - generated config files
#   - merge artifacts that show up in shortlog touches
_AUTOGEN_PATTERNS = (".pb.go", "_pb.go", "buf-wrapper.yaml", ".pb.gw.go")

# Dependency-management files that show as a contributor's "top files" but
# only represent vendor/dep bumps, not domain expertise. These distort the
# primary-focus derivation, so we drop them before path-matching.
_DEPENDENCY_PATHS = {
    "go.mod",
    "go.sum",
    "vendor/modules.txt",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "requirements.txt",
    ".gitignore",
    "Makefile",
    "README.md",
    "CHANGELOG.md",
    "CODEOWNERS",
}
# Subject substrings that indicate a non-substantive commit. Filtering these
# from the LLM input prevents the model from latching onto noise like
# "email blast 2" as if it were a feature being shipped.
_NOISE_SUBJECT_PATTERNS = (
    "merge branch ",
    "merge pull request ",
    "merge remote-tracking branch ",
)
# Drop subjects that are too short to carry meaning (often log lines, file
# names, or marker commits like "next list").
_MIN_SUBJECT_LEN = 16


def _is_autogen_path(path: str) -> bool:
    p = (path or "").lower()
    if any(p.endswith(pat) or pat in p for pat in _AUTOGEN_PATTERNS):
        return True
    if path in _DEPENDENCY_PATHS:
        return True
    # vendor/ paths represent either dependency bumps or rare hot-patches —
    # not the engineer's core domain. Drop them so primary_focus surfaces
    # actual gobroker code instead.
    if p.startswith("vendor/"):
        return True
    return False


def _is_noise_subject(subject: str) -> bool:
    s = (subject or "").strip().lower()
    if len(s) < _MIN_SUBJECT_LEN:
        return True
    return any(s.startswith(p) for p in _NOISE_SUBJECT_PATTERNS)


# Generic teams that virtually everyone belongs to — unhelpful as a
# disambiguator in the summary, so we strip them from the team list.
_GENERIC_TEAM_NAMES = {"engineering", "read only members", "members", "oss admin"}


# Path-prefix -> human-readable domain. Order matters — first match wins.
# This is the deterministic part of the summary: the LLM can't be trusted
# to derive a clean phrase from a path with a 3B model, so we inject the
# right phrase directly into the prompt as a hard directive.
_PATH_DOMAIN_MAP: list[tuple[str, str]] = [
    ("workers/asset/options/", "options asset workers"),
    ("workers/asset/", "asset background workers"),
    ("workers/order/", "order processing workers"),
    ("workers/", "background workers"),
    ("rest/api/binder/", "the account-binding REST flow"),
    ("rest/api/controller/owner/", "owner / account-ownership APIs"),
    ("rest/api/controller/cashinterest/", "the cash-interest REST API"),
    ("rest/api/controller/", "REST API controllers"),
    ("rest/api/middleware/", "REST middleware"),
    ("rest/api/", "the REST API layer"),
    ("rest/", "the REST layer"),
    ("service/portfoliov2/", "the portfolio v2 service"),
    ("service/pdfgenerator/", "the PDF / statement generation pipeline"),
    ("service/account/", "the account service"),
    ("service/brokeraccount/", "the broker-account service"),
    ("service/order/", "the order service"),
    ("service/", "core gobroker services"),
    ("external/ledgie/", "the ledger (ledgie) integration"),
    ("external/plaid/", "the Plaid bank-link integration"),
    ("external/", "third-party integrations"),
    ("oms/", "the order-management system (OMS)"),
    ("trading/", "trading-engine code"),
    ("mailer/", "transactional email"),
    ("models/enum/", "the enum / lookup tables"),
    ("models/", "the gobroker domain models"),
    ("entities/", "the entities layer"),
    ("cmd/gbutil/cmd/documents/", "tax-document tooling"),
    ("cmd/gbutil/cmd/mailer/", "the bulk-email tooling"),
    ("cmd/gbutil/", "CLI ops tooling (gbutil)"),
    ("cmd/", "CLI tooling"),
    ("migrations/", "database migrations"),
    ("schema/", "database schema definitions"),
    ("gbutil/", "gobroker utility code"),
    ("clearing/", "post-trade clearing"),
    ("test/", "the integration test suite"),
]


def _derive_primary_focus(top_files: list[dict]) -> str:
    """Return a human-readable phrase for the engineer's primary domain,
    derived from their highest-touched file path. Empty if no clean match.

    Pre-computing this server-side lets us pin the LLM's lead sentence to
    factual data, instead of trusting the 3B model not to template-lock on
    a phrase from the prompt examples."""
    if not top_files:
        return ""
    top_path = (top_files[0].get("path") or "").lower()
    for prefix, phrase in _PATH_DOMAIN_MAP:
        if top_path.startswith(prefix):
            return phrase
    head = top_path.split("/", 1)[0]
    if head:
        return f"{head}/ code"
    return ""


def _derive_secondary_focuses(top_files: list[dict]) -> list[str]:
    """Distinct secondary domains from the next few top files.
    Used so the 2nd sentence has explicit material to draw from."""
    seen = set()
    out: list[str] = []
    for f in top_files[1:8]:
        path = (f.get("path") or "").lower()
        for prefix, phrase in _PATH_DOMAIN_MAP:
            if path.startswith(prefix):
                if phrase not in seen:
                    seen.add(phrase)
                    out.append(phrase)
                break
        if len(out) >= 4:
            break
    return out


def _build_signals(detail: dict) -> dict:
    """Project a contributor detail dict down to the minimum LLM input.
    Filters out auto-generated paths and noise commits so the model isn't
    fed signal-free input that it then tries to summarize."""
    s = detail.get("stats") or {}
    teams = [t.get("name") for t in (detail.get("teams") or []) if t.get("name")]
    teams = [t for t in teams if t.lower() not in _GENERIC_TEAM_NAMES]

    top_files = []
    for f in (detail.get("top_files") or []):
        path = f.get("path") or ""
        if _is_autogen_path(path):
            continue
        top_files.append({"path": path, "commits": f.get("commits", 0)})
        if len(top_files) >= 12:
            break

    subjects = []
    seen = set()
    for c in (detail.get("recent_commits") or []):
        subj = (c.get("subject") or "").strip()
        if not subj or _is_noise_subject(subj) or subj in seen:
            continue
        seen.add(subj)
        subjects.append(subj)
        if len(subjects) >= 15:
            break

    return {
        "name": detail.get("name") or detail.get("login"),
        "first_name": (detail.get("name") or detail.get("login") or "").split()[0],
        "login": detail.get("login"),
        "in_org": bool(detail.get("in_org")),
        "teams": teams,
        "stats": {
            "total_commits": s.get("total_commits", 0),
            "commits_30d": s.get("commits_30d", 0),
            "commits_90d": s.get("commits_90d", 0),
            "first_commit": (s.get("first_commit") or "")[:10],
            "last_commit": (s.get("last_commit") or "")[:10],
        },
        "top_files": top_files,
        "recent_commit_subjects": subjects,
        # Pre-derived directives. The 3B model is unreliable at deriving
        # phrases from paths, so we hand it the answer.
        "primary_focus": _derive_primary_focus(top_files),
        "secondary_focuses": _derive_secondary_focuses(top_files),
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
