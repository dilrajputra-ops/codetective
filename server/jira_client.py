"""Jira ticket fetch via `acli` CLI, with ADF flattening and on-disk caching.

Business intent lives in Jira ticket descriptions, not commit bodies. This module
fetches titles/descriptions for a set of Jira keys so the narrative LLM can
ground "why" claims in actual ticket content.

Degrades gracefully: if `acli` is missing, unconfigured, or times out, we return
stub records (id + error) so the synth pipeline keeps going.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import CACHE_DIR

JIRA_CACHE_DIR = CACHE_DIR / "jira"
JIRA_CACHE_TTL = 24 * 60 * 60  # tickets rarely change; 24h is fine
_ACLI_TIMEOUT = 6  # cold acli fetch is ~3s; allow headroom


def _cache_path(key: str) -> Path:
    h = hashlib.sha1(key.encode()).hexdigest()
    return JIRA_CACHE_DIR / f"{h}.json"


def _cache_read(key: str) -> dict | None:
    f = _cache_path(key)
    if not f.exists():
        return None
    try:
        if time.time() - f.stat().st_mtime > JIRA_CACHE_TTL:
            return None
        return json.loads(f.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _cache_write(key: str, data: dict) -> None:
    try:
        JIRA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(key).write_text(json.dumps(data))
    except OSError:
        pass


def _adf_to_text(node: Any) -> str:
    """Flatten Atlassian Document Format (ADF) nested content into plain text.

    Jira descriptions are returned as ADF JSON — a tree of paragraph/text nodes.
    We recurse and concatenate `text` leaves, inserting newlines between block
    nodes so paragraphs stay separated.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "\n".join(filter(None, (_adf_to_text(n) for n in node)))
    if not isinstance(node, dict):
        return ""
    # Text leaf: just return its text.
    if node.get("type") == "text":
        return node.get("text", "")
    # Block nodes with content: walk children, separator depends on type.
    content = node.get("content") or []
    inner = "".join(_adf_to_text(c) if (isinstance(c, dict) and c.get("type") == "text")
                    else _adf_to_text(c)
                    for c in content)
    # Paragraphs/headings become newline-separated chunks.
    if node.get("type") in ("paragraph", "heading", "bulletList", "orderedList", "listItem", "codeBlock"):
        return inner + "\n"
    return inner


def _fetch_one(key: str, timeout: int = _ACLI_TIMEOUT) -> dict:
    """Return {id, summary, description, issuetype, status, error}. Never raises."""
    cached = _cache_read(key)
    if cached is not None:
        return cached

    out: dict = {
        "id": key,
        "summary": "",
        "description": "",
        "issuetype": "",
        "status": "",
        "error": None,
    }
    try:
        proc = subprocess.run(
            [
                "acli", "jira", "workitem", "view", key, "--json",
                "--fields", "summary,description,issuetype,status",
            ],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            out["error"] = (proc.stderr.strip() or "acli failed")[:200]
            # Cache negative results too, but briefly (short-circuit repeat misses).
            _cache_write(key, out)
            return out
        data = json.loads(proc.stdout)
        fields = data.get("fields") or {}
        out["summary"] = (fields.get("summary") or "").strip()
        desc_text = _adf_to_text(fields.get("description"))
        out["description"] = desc_text.strip()
        out["issuetype"] = ((fields.get("issuetype") or {}).get("name") or "").strip()
        out["status"] = ((fields.get("status") or {}).get("name") or "").strip()
    except subprocess.TimeoutExpired:
        out["error"] = "acli timeout"
    except FileNotFoundError:
        out["error"] = "acli not installed"
    except (json.JSONDecodeError, OSError) as e:
        out["error"] = str(e)[:200]
    _cache_write(key, out)
    return out


def fetch_many(keys: list[str], max_workers: int = 5) -> list[dict]:
    """Fetch multiple tickets in parallel. Preserves input order, dedupes."""
    if not keys:
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            ordered.append(k)

    from concurrent.futures import ThreadPoolExecutor
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(ordered))) as ex:
        futs = {ex.submit(_fetch_one, k): k for k in ordered}
        for fut in futs:
            k = futs[fut]
            try:
                results[k] = fut.result(timeout=_ACLI_TIMEOUT + 1)
            except Exception as e:
                results[k] = {"id": k, "summary": "", "description": "", "issuetype": "", "status": "", "error": str(e)[:200]}
    return [results[k] for k in ordered]
