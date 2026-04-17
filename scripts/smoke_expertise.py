"""Smoke test for DOK-lite contributor scoring.

Usage:
    python scripts/smoke_expertise.py                  # auto-pick 3 paths
    python scripts/smoke_expertise.py path1 path2 ...  # explicit paths

Run from the repo root with the venv active. Loads .env so GOBROKER_PATH resolves.
"""
from __future__ import annotations

import os
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from server import synth
from server.config import GOBROKER_PATH


def auto_pick_paths(n: int = 3) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "*.go"],
        cwd=str(GOBROKER_PATH), capture_output=True, text=True, timeout=10,
    ).stdout.splitlines()
    if not out:
        out = subprocess.run(
            ["git", "ls-files"],
            cwd=str(GOBROKER_PATH), capture_output=True, text=True, timeout=10,
        ).stdout.splitlines()
    random.seed(42)
    random.shuffle(out)
    return out[:n]


def fmt_breakdown(b: dict) -> str:
    return (
        f"blame={b.get('blame_share', 0):.2f} "
        f"recency={b.get('recency', 0):.2f} "
        f"author={b.get('authorship', 0):.0f} "
        f"vol={b.get('volume', 0):.2f}"
        + (" DEPARTED" if b.get("departed") else "")
    )


def run(paths: list[str]) -> int:
    failures = 0
    for p in paths:
        print(f"\n=== {p} ===")
        try:
            case = synth.investigate(p, None)
        except FileNotFoundError as e:
            print(f"  SKIP: {e}")
            continue
        contributors = case["contributors"]
        if not contributors:
            print("  no contributors returned")
            continue

        scores = [c["score"] for c in contributors]
        if scores != sorted(scores, reverse=True):
            print(f"  FAIL: scores not sorted DESC -> {scores}")
            failures += 1

        active_seen_after_departed = False
        seen_departed = False
        for c in contributors:
            if c.get("is_departed"):
                seen_departed = True
            elif seen_departed:
                active_seen_after_departed = True
        if active_seen_after_departed:
            print("  FAIL: an active contributor ranks below a departed one")
            failures += 1

        for i, c in enumerate(contributors[:5], 1):
            tag = " [DEP]" if c.get("is_departed") else ""
            print(
                f"  {i}. {c['name'][:30]:30s} {c['role']:22s} "
                f"score={c['score']:>5.2f} "
                f"{fmt_breakdown(c['score_breakdown'])} "
                f"lines={c['lines']}{tag}"
            )
    return failures


def main() -> int:
    paths = sys.argv[1:] or auto_pick_paths(3)
    if not paths:
        print("No paths to test (gobroker repo empty or unreachable).")
        return 1
    print(f"Smoke-testing expertise scoring against {len(paths)} paths in {GOBROKER_PATH}")
    return 1 if run(paths) else 0


if __name__ == "__main__":
    sys.exit(main())
