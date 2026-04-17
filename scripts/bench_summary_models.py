"""Head-to-head benchmark of local LLMs on the contributor-summary task.

Hits the existing /api/contributors/{login} endpoint to get real signals, then
calls each candidate model directly with the same SYSTEM prompt + signals so
the only variable is the model.

Usage:
    venv/bin/python scripts/bench_summary_models.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.contributor_summary import SYSTEM, SCHEMA_HINT, _build_signals  # noqa: E402

OLLAMA_HOST = "http://127.0.0.1:11434"
SERVER_HOST = "http://127.0.0.1:8765"

# Candidates locally available. Add more after `ollama pull <model>`.
MODELS = [
    "qwen2.5-coder:3b",
    "qwen2.5-coder:7b",
    "llama3.2:3b",
]

# Diverse engineer profiles to stress-test.
LOGINS = [
    ("deandiakov", "Ledger / options / mixed domains"),
    ("sachyco", "REST account-binding (top file is binder.go)"),
    ("gyturi1", "Identity team, enum-heavy"),
    ("lszamosi", "REST middleware + portfolio v2 (lots of vendor noise)"),
    ("umitanuki", "Departed contributor, ledger era"),
]


def fetch_detail(login: str) -> dict:
    with urllib.request.urlopen(f"{SERVER_HOST}/api/contributors/{login}", timeout=15) as r:
        return json.loads(r.read())


def call_model(model: str, signals: dict, timeout: int = 90) -> tuple[dict | None, float]:
    user = json.dumps({"signals": signals, "output_schema": SCHEMA_HINT}, default=str)[:6000]
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user},
        ],
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
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return {"error": str(e)}, time.time() - t0
    elapsed = time.time() - t0
    content = (raw.get("message") or {}).get("content") or ""
    try:
        return json.loads(content), elapsed
    except json.JSONDecodeError:
        return {"error": "invalid JSON", "raw": content[:300]}, elapsed


def main() -> None:
    # Warm each model once so cold-load latency doesn't poison the comparison.
    print("Warming models...")
    for m in MODELS:
        warm_body = json.dumps({"model": m, "prompt": "ok", "stream": False, "keep_alive": "30m"}).encode()
        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    f"{OLLAMA_HOST}/api/generate", data=warm_body,
                    headers={"Content-Type": "application/json"}, method="POST",
                ),
                timeout=120,
            ).read()
            print(f"  {m}: warmed")
        except Exception as e:
            print(f"  {m}: WARM FAILED -> {e}")

    print("\n" + "=" * 80)
    for login, label in LOGINS:
        try:
            detail = fetch_detail(login)
        except Exception as e:
            print(f"\n[{login}] failed to fetch detail: {e}")
            continue
        signals = _build_signals(detail)
        print(f"\n# {detail.get('name')} (@{login}) — {label}")
        print(f"  primary_focus={signals['primary_focus']!r}")
        print(f"  secondary={signals['secondary_focuses']}")
        print(f"  teams={signals['teams']}  total={signals['stats']['total_commits']} 30d={signals['stats']['commits_30d']} 90d={signals['stats']['commits_90d']}")
        print()
        for model in MODELS:
            data, elapsed = call_model(model, signals)
            tag = f"[{model}] {elapsed:5.1f}s"
            if data.get("error"):
                print(f"{tag} ERROR: {data['error']}")
                continue
            summary = data.get("summary", "")
            focus = data.get("focus_areas") or []
            themes = data.get("recent_themes") or []
            print(f"{tag}")
            print(f"  summary: {summary}")
            print(f"  focus:   {focus}")
            if themes:
                for t in themes:
                    print(f"  theme:   {t}")
            print()
        print("-" * 80)


if __name__ == "__main__":
    main()
