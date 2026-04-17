# Codetective — Agent Development Guidance

Read this before making changes. It encodes scope decisions and design language we've already agreed on, so future sessions don't drift.

## Scope guardrails (do NOT cross)

- **Single repo only**: gobroker (`~/Documents/alpaca/gobroker`). No multi-repo, no repo dropdown, no path-prefix parsing. v2 problem.
- **One screen, one input, one result**. No demo chips, no preset workflows, no multi-step "investigate" wizard. Paste path → get answer.
- **Not Pathfinder**. If a feature smells like Onyx search, system topology, or scoped re-querying, it doesn't belong here. Codetective is deterministic + 1 local LLM call.
- **No chatbot, no digital twin, no NL Q&A.**
- **No frontend framework**. Single static `index.html` + vanilla JS + `fetch()`. No React, no build step.
- **LOCAL ONLY. No cloud LLM, no cloud embeddings.** Codetective reads proprietary org metadata (paths, author names, commit subjects, Jira IDs from a private repo). Nothing leaves the machine. No OpenAI, no Anthropic, no hosted Llama API, no third-party embedding service. If a contributor proposes a cloud LLM, reject the PR.

## Architecture (locked)

- FastAPI single service, port 8765.
- Endpoints: `GET /` (serves index.html), `GET /health`, `POST /investigate`, `POST /reindex`.
- All git ops shellout to local gobroker clone. All open-PR data via `gh` CLI (still local, just talks to GitHub on the user's behalf).
- Exactly **one** local LLM call per investigation via Ollama (`llama3.2:3b` by default, JSON mode), scoped to summary copy + why bullets + next-step. Everything else is deterministic. Always has a templated fallback when Ollama is down/slow.
- Vector index over gobroker file paths via Ollama embeddings (`nomic-embed-text`), stored in a single SQLite table at `VECTOR_DB`. Brute-force cosine similarity is fine for ~10-30k paths. Powers the "Similar files / who else might know" card. Built once via `POST /reindex`; idempotent.
- On-disk cache for `gh` results (`/tmp/codemap-cache`, 10 min TTL). Never let rate-limit kill the demo.

## Required local services

- **Ollama** running at `OLLAMA_HOST` (default `http://localhost:11434`). Install via `brew install --cask ollama`, then `ollama pull llama3.2:3b nomic-embed-text`.
- **gh CLI** authed (`gh auth status`). Used for open-PR lookups.
- **gobroker clone** at `GOBROKER_PATH`.

If any of these are missing, Codetective degrades gracefully (templated narrative, empty open-PR list, empty similar-files list) and surfaces the degradation in the Sources footer.

## Data sources (in order of trust)

1. `.github/CODEOWNERS` — primary ownership signal.
2. `git blame -L start,end` — who actually wrote the code in scope.
3. `git log --follow -n 20` — recent activity, survives renames.
4. `gh api search/issues` + `gh pr view` — open PRs touching path.
5. Regex `\b[A-Z]{2,8}-\d+\b` over PR titles, branch names, commit subjects/bodies — Jira IDs.

If any source degrades, surface it honestly in the Sources footer. Never silently fall back.

## UI design language

See `AGENTS.local.md` (gitignored) for design references and anti-patterns.

## Code conventions

- Python 3.14, FastAPI, no Django.
- All shellouts: `cwd=GOBROKER_PATH`, explicit `timeout=`, no `shell=True`.
- LLM call: 5s timeout, JSON mode, defensive parsing, always templated fallback.
- Path validation: must resolve under `GOBROKER_PATH`, return 400/404 if not.
- No comments narrating obvious code. Only explain non-obvious intent or trade-offs.
- Small files. If a module crosses ~150 lines, split it.

## What NOT to add (anti-scope)

- Cloud LLM, cloud embeddings, any external AI API.
- Auth, sessions, users, app-level DB (the SQLite vector store doesn't count — it's a cache).
- Multi-repo support.
- Slack/Confluence/Jira API calls (Jira IDs are link-only).
- Admin UI, settings page.
- Tests beyond a smoke script (hackathon scope).

## Demo invariant

Server start → open `http://127.0.0.1:8765` → paste any gobroker-relative path → result in <3s. If a change breaks this, revert.
