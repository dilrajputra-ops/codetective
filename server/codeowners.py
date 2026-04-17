"""Parse and match GitHub CODEOWNERS for gobroker."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from wcmatch import glob

from .config import GOBROKER_PATH

CANDIDATES = ["CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"]


def _find_codeowners() -> Optional[Path]:
    for c in CANDIDATES:
        p = GOBROKER_PATH / c
        if p.is_file():
            return p
    return None


def _parse(text: str) -> list[tuple[int, str, list[str]]]:
    """Returns [(line_no, pattern, owners)] in file order."""
    rules: list[tuple[int, str, list[str]]] = []
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        pattern = parts[0]
        owners = [p for p in parts[1:] if p.startswith("@") or "@" in p]
        rules.append((i, pattern, owners))
    return rules


def _glob_match(pattern: str, path: str) -> bool:
    """GitHub CODEOWNERS glob semantics. Roughly: leading / anchors, trailing / matches dir contents."""
    p = pattern
    if p.startswith("/"):
        p = p[1:]
    else:
        # un-anchored patterns match anywhere
        if "/" not in p.rstrip("/"):
            p = "**/" + p
    if p.endswith("/"):
        p = p + "**"
    return glob.globmatch(path, p, flags=glob.GLOBSTAR)


def match(path: str) -> dict:
    """Return owners for `path` (gobroker-relative). Last matching rule wins."""
    f = _find_codeowners()
    if not f:
        return {"owners": [], "rule": None, "line": None, "source": None}

    text = f.read_text(encoding="utf-8", errors="replace")
    rules = _parse(text)
    hit: Optional[tuple[int, str, list[str]]] = None
    for rule in rules:
        if _glob_match(rule[1], path):
            hit = rule
    if not hit:
        return {"owners": [], "rule": None, "line": None, "source": str(f.relative_to(GOBROKER_PATH))}
    line_no, pattern, owners = hit
    return {
        "owners": owners,
        "rule": pattern,
        "line": line_no,
        "source": str(f.relative_to(GOBROKER_PATH)),
    }


def match_with_inference(path: str) -> dict:
    """Like match() but if the exact path is unowned, walk up parent directories
    to find the nearest CODEOWNERS rule. Marks `inferred: True` when used.

    Why: gobroker CODEOWNERS often only covers sub-paths (e.g. `rest/api/`) and
    leaves package roots like `rest/rest.go` unowned. The nearest parent is
    almost always the right team.
    """
    direct = match(path)
    if direct["owners"]:
        return {**direct, "inferred": False, "inferred_from": None}

    parts = path.split("/")
    for i in range(len(parts) - 1, 0, -1):
        parent = "/".join(parts[:i]) + "/"
        m = match(parent)
        if m["owners"]:
            return {**m, "inferred": True, "inferred_from": parent}

    return {**direct, "inferred": False, "inferred_from": None}


def short_team_name(owner: str) -> str:
    """@alpacahq/identity -> Identity"""
    if not owner:
        return ""
    name = owner.rsplit("/", 1)[-1]
    return name.replace("-", " ").replace("_", " ").title()
