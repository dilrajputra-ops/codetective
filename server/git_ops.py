"""Thin git wrappers scoped to the gobroker repo. All shellouts have a timeout."""
from __future__ import annotations

import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import GOBROKER_PATH


def _run(args: list[str], timeout: int = 5) -> str:
    out = subprocess.run(
        args,
        cwd=str(GOBROKER_PATH),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return out.stdout


def file_exists(path: str) -> bool:
    full = (GOBROKER_PATH / path).resolve()
    try:
        full.relative_to(GOBROKER_PATH)
    except ValueError:
        return False
    return full.is_file()


def read_file(path: str, max_bytes: int = 1_000_000, max_lines: int = 2000) -> dict:
    """Return file content for the visual range picker. Hard caps for safety."""
    full = (GOBROKER_PATH / path).resolve()
    try:
        full.relative_to(GOBROKER_PATH)
    except ValueError:
        raise FileNotFoundError(path)
    if not full.is_file():
        raise FileNotFoundError(path)
    raw = full.read_bytes()[:max_bytes]
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()[:max_lines]
    return {
        "path": path,
        "lines": lines,
        "total_lines": len(text.splitlines()),
        "truncated": len(raw) >= max_bytes or len(text.splitlines()) > max_lines,
    }


def blame(path: str, start: Optional[int] = None, end: Optional[int] = None) -> dict:
    """Return per-author aggregates and per-author code line snippets in the range."""
    args = ["git", "blame", "--line-porcelain", "-w", "-M", "-C"]
    if start and end:
        args += ["-L", f"{start},{end}"]
    args += ["--", path]
    try:
        text = _run(args, timeout=8)
    except subprocess.TimeoutExpired:
        return {"authors": [], "lines_by_author": {}}

    by_author: dict[str, dict] = defaultdict(
        lambda: {"name": "", "email": "", "lines": 0, "last_date": ""}
    )
    lines_by_author: dict[str, list[dict]] = defaultdict(list)

    cur_name = cur_email = cur_date = cur_sha = ""
    cur_line_no = start or 1
    for line in text.split("\n"):
        if not line:
            continue
        if line[0] != "\t":
            parts = line.split(" ", 1)
            tag = parts[0]
            rest = parts[1] if len(parts) > 1 else ""
            if len(tag) == 40 and all(c in "0123456789abcdef" for c in tag):
                cur_sha = tag
                # rest like "82 82 1" -> original_line final_line group_size
                bits = rest.split()
                if len(bits) >= 2:
                    try:
                        cur_line_no = int(bits[1])
                    except ValueError:
                        pass
            elif tag == "author":
                cur_name = rest.strip()
            elif tag == "author-mail":
                cur_email = rest.strip().strip("<>")
            elif tag == "author-time":
                try:
                    cur_date = datetime.fromtimestamp(int(rest), tz=timezone.utc).isoformat()
                except ValueError:
                    cur_date = ""
        else:
            key = cur_email or cur_name
            rec = by_author[key]
            rec["name"] = cur_name
            rec["email"] = cur_email
            rec["lines"] += 1
            if cur_date > rec["last_date"]:
                rec["last_date"] = cur_date
            if len(lines_by_author[key]) < 5:
                lines_by_author[key].append({
                    "line_no": cur_line_no,
                    "sha": cur_sha[:7],
                    "code": line[1:][:140],
                })

    authors = sorted(by_author.values(), key=lambda r: -r["lines"])
    return {
        "authors": authors,
        "lines_by_author": {k: v for k, v in lines_by_author.items()},
    }


def log(path: str, limit: int = 20) -> list[dict]:
    """Recent commits touching the path; --follow survives renames."""
    fmt = "%H%x09%an%x09%ae%x09%aI%x09%s"
    args = ["git", "log", "--follow", f"-n{limit}", f"--pretty=format:{fmt}", "--", path]
    try:
        text = _run(args, timeout=6)
    except subprocess.TimeoutExpired:
        args = ["git", "log", f"-n{limit}", f"--pretty=format:{fmt}", "--", path]
        try:
            text = _run(args, timeout=4)
        except subprocess.TimeoutExpired:
            return []

    commits: list[dict] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        sha, author, email, date, subject = parts
        commits.append(
            {
                "sha": sha,
                "author": author,
                "email": email,
                "date": date,
                "subject": subject,
            }
        )
    return commits


def commit_body(sha: str) -> str:
    try:
        return _run(["git", "show", "-s", "--format=%B", sha], timeout=3).strip()
    except subprocess.TimeoutExpired:
        return ""


def first_author(path: str) -> dict:
    """Person who first added the file (for the authorship-bonus signal). Empty dict on failure."""
    fmt = "%H%x09%an%x09%ae%x09%aI"
    args = [
        "git", "log", "--diff-filter=A", "--follow", "--reverse",
        "-n1", f"--pretty=format:{fmt}", "--", path,
    ]
    try:
        text = _run(args, timeout=4).strip()
    except subprocess.TimeoutExpired:
        return {}
    if not text:
        return {}
    parts = text.split("\t")
    if len(parts) != 4:
        return {}
    sha, name, email, date = parts
    return {"sha": sha, "name": name, "email": email, "date": date}


def commits_with_stats(path: str, limit: int = 50) -> list[dict]:
    """Like log() but with per-commit lines_added/lines_deleted from --numstat.

    Skips merges to avoid double-counting cross-branch noise.
    """
    fmt = "%H%x09%an%x09%ae%x09%aI%x09%s"
    args = [
        "git", "log", "--follow", "--no-merges", f"-n{limit}",
        "--numstat", f"--pretty=format:{fmt}", "--", path,
    ]
    try:
        text = _run(args, timeout=8)
    except subprocess.TimeoutExpired:
        args = [a for a in args if a != "--follow"]
        try:
            text = _run(args, timeout=5)
        except subprocess.TimeoutExpired:
            return []

    commits: list[dict] = []
    cur: Optional[dict] = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if len(line) > 40 and line[40] == "\t" and all(c in "0123456789abcdef" for c in line[:40]):
            parts = line.split("\t", 4)
            if len(parts) == 5:
                sha, author, email, date, subject = parts
                cur = {
                    "sha": sha, "author": author, "email": email,
                    "date": date, "subject": subject,
                    "lines_added": 0, "lines_deleted": 0, "files_changed": 0,
                }
                commits.append(cur)
            continue
        if cur is None:
            continue
        bits = line.split("\t")
        if len(bits) < 3:
            continue
        try:
            added = int(bits[0]) if bits[0] != "-" else 0
            deleted = int(bits[1]) if bits[1] != "-" else 0
        except ValueError:
            continue
        cur["lines_added"] += added
        cur["lines_deleted"] += deleted
        cur["files_changed"] += 1
    return commits


_DEPARTED_FILE = Path(__file__).resolve().parent.parent / "departed.txt"
_departed_cache: Optional[list[str]] = None


def read_departed() -> list[str]:
    """Load case-insensitive substring patterns from departed.txt. Cached per process."""
    global _departed_cache
    if _departed_cache is not None:
        return _departed_cache
    patterns: list[str] = []
    try:
        for raw in _DEPARTED_FILE.read_text(encoding="utf-8").splitlines():
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            patterns.append(s.lower())
    except FileNotFoundError:
        pass
    _departed_cache = patterns
    return patterns
