"""Find the file path + line range for a pasted code snippet.

Strategy: pick the most distinctive line(s) from the pasted text, run
`git grep -n -F` against gobroker for them, and return ranked matches.

Distinctive = long enough to be unique, not pure boilerplate
(`return nil`, `}`, imports, etc.). We try the longest candidate first;
if it has too many hits or none, fall back to the next.

Cheap and deterministic — no LLM, no embeddings. ~50-200ms typical.
"""
from __future__ import annotations

import re
import subprocess
import time
from collections import defaultdict
from pathlib import Path

from .config import GOBROKER_PATH

# Lines we never bother grepping for — they match thousands of files
# and produce noise. Add patterns conservatively; the goal is to skip
# only stuff that's truly meaningless as a fingerprint.
_BOILERPLATE_RE = re.compile(
    r"^\s*("
    r"return( nil| err| error|;)?\s*$|"
    r"continue\s*$|break\s*$|"
    r"package\s+\w+\s*$|"
    r"import\s*\(?\s*$|"
    r"\}\s*$|\)\s*$|\{\s*$|"
    r"if\s+err\s*!=\s*nil\s*\{?\s*$|"
    r"\s*$"
    r")"
)

# Short comments (license headers, TODOs, "// ..." stubs) match thousands of
# files. Long distinctive comments (doc strings, function descriptions) are
# excellent fingerprints. Treat short comments only as boilerplate.
_SHORT_COMMENT_RE = re.compile(r"^\s*//.{0,24}$")

# Min line length to be worth grepping. Below this, false-positive rate
# is too high in a 2M-line monorepo.
_MIN_LINE_LEN = 16

# Stop spending time after this many candidate lines have been tried.
_MAX_CANDIDATES = 4

# Hard cap on results returned, to keep the UI tidy.
_MAX_RESULTS = 8

# If a single line matches more than this many files, treat as too generic
# and try the next candidate instead. (Still keep results if no candidate
# has fewer hits.)
_NOISE_HIT_THRESHOLD = 60

# In-memory result cache. Snippets pasted twice in a row (typo correction,
# clipboard double-paste) shouldn't re-grep.
_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 60


def _candidate_lines(snippet: str) -> list[str]:
    """Return paste lines ranked from most-distinctive to least, filtering
    out boilerplate and short lines."""
    lines = []
    for raw in snippet.splitlines():
        stripped = raw.rstrip("\r")
        body = stripped.strip()
        if len(body) < _MIN_LINE_LEN:
            continue
        if _BOILERPLATE_RE.match(stripped):
            continue
        if _SHORT_COMMENT_RE.match(stripped):
            continue
        lines.append(stripped)
    # Longer lines are statistically more distinctive in a Go monorepo
    # (function bodies, struct literals, error messages).
    lines.sort(key=lambda s: -len(s.strip()))
    # Dedupe while preserving order — pasted code with duplicate lines
    # would otherwise waste candidate slots.
    seen, out = set(), []
    for l in lines:
        key = l.strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(l)
    return out[:_MAX_CANDIDATES]


def _git_grep(needle: str, timeout: int = 6) -> list[tuple[str, int, str]]:
    """Run `git grep -n -F` for an exact-string match. Returns
    [(path, line_no, line_text), ...]. Empty on no match or error.

    -F: fixed string (no regex interpretation, safe for any pasted code)
    -n: include line numbers
    --untracked: include untracked files too (so freshly written code shows)
    """
    try:
        out = subprocess.run(
            ["git", "grep", "-n", "-F", "--no-color", "--untracked", "--",
             needle.strip()],
            cwd=str(GOBROKER_PATH),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return []
    if out.returncode not in (0, 1):
        # 0 = match, 1 = no match. Anything else = git error.
        return []

    results = []
    for raw_line in out.stdout.splitlines():
        # Format: path:lineno:content
        parts = raw_line.split(":", 2)
        if len(parts) < 3:
            continue
        path, lineno_s, content = parts
        try:
            lineno = int(lineno_s)
        except ValueError:
            continue
        results.append((path, lineno, content))
    return results


def _read_window(path: str, line_no: int, before: int = 2, after: int = 2) -> str:
    """Pull a small preview window of lines around the matched line so the
    UI can show context without a separate file-read round-trip."""
    full = GOBROKER_PATH / path
    try:
        text = full.read_text(errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()
    start = max(0, line_no - 1 - before)
    end = min(len(lines), line_no - 1 + after + 1)
    return "\n".join(lines[start:end])


def _expand_range(snippet_lines: list[str], path: str, hit_line: int) -> tuple[int, int]:
    """If the user pasted multiple lines, try to anchor the END of the
    range by walking forward from the hit line and matching subsequent
    snippet lines. Returns (start, end). Falls back to (hit_line, hit_line).

    Only confirms lines that match exactly (after strip), so this is
    conservative but accurate for verbatim pastes."""
    if len(snippet_lines) <= 1:
        return hit_line, hit_line

    full = GOBROKER_PATH / path
    try:
        file_lines = full.read_text(errors="replace").splitlines()
    except OSError:
        return hit_line, hit_line

    # Find which snippet line the hit corresponds to. Match by stripped
    # content of the hit line against snippet lines.
    if hit_line - 1 >= len(file_lines):
        return hit_line, hit_line
    hit_text = file_lines[hit_line - 1].strip()
    snippet_idx = -1
    for i, s in enumerate(snippet_lines):
        if s.strip() == hit_text:
            snippet_idx = i
            break
    if snippet_idx == -1:
        return hit_line, hit_line

    # Walk backward in both snippet and file to find start.
    start_file = hit_line
    start_snip = snippet_idx
    while start_snip > 0 and start_file > 1:
        if file_lines[start_file - 2].strip() == snippet_lines[start_snip - 1].strip():
            start_file -= 1
            start_snip -= 1
        else:
            break

    # Walk forward.
    end_file = hit_line
    end_snip = snippet_idx
    while end_snip < len(snippet_lines) - 1 and end_file < len(file_lines):
        if file_lines[end_file].strip() == snippet_lines[end_snip + 1].strip():
            end_file += 1
            end_snip += 1
        else:
            break

    return start_file, end_file


def find(snippet: str) -> dict:
    """Main entry. Returns {matches: [...], queried: str | None,
    note: str | None}."""
    if not snippet or not snippet.strip():
        return {"matches": [], "queried": None, "note": "empty snippet"}

    cache_key = snippet.strip()[:5000]
    now = time.time()
    cached = _CACHE.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL:
        return {"matches": cached[1], "queried": "cached", "note": None}

    candidates = _candidate_lines(snippet)
    if not candidates:
        return {
            "matches": [],
            "queried": None,
            "note": "snippet has no distinctive lines (too short or pure boilerplate)",
        }

    snippet_lines = [l for l in snippet.splitlines() if l.strip()]
    aggregated: dict[tuple[str, int], dict] = {}
    queried_line = None

    for cand in candidates:
        hits = _git_grep(cand)
        if not hits:
            continue
        # If the candidate matched everywhere, skip and try the next one
        # — but only if a later candidate exists. Otherwise return what
        # we have.
        if len(hits) > _NOISE_HIT_THRESHOLD and cand != candidates[-1]:
            continue
        queried_line = cand
        for path, lineno, content in hits[:_MAX_RESULTS]:
            key = (path, lineno)
            if key in aggregated:
                aggregated[key]["score"] += 1
                continue
            start, end = _expand_range(snippet_lines, path, lineno)
            preview = _read_window(path, lineno, before=1, after=1)
            aggregated[key] = {
                "path": path,
                "line_start": start,
                "line_end": end,
                "match_line": lineno,
                "preview": preview,
                "matched_text": content.strip()[:200],
                "score": 1 + (end - start),
            }
        if aggregated:
            break  # first productive candidate wins; don't dilute results

    # Rank: highest score (multi-line confirmed > single-line)
    matches = sorted(aggregated.values(), key=lambda m: -m["score"])[:_MAX_RESULTS]
    _CACHE[cache_key] = (now, matches)
    return {
        "matches": matches,
        "queried": queried_line,
        "note": None if matches else "no matches found in gobroker",
    }
