# Codetective — Feature List

The complete catalog of what Codetective does today, grouped by surface.
Use this as the canonical reference for demos, submissions, and roadmap discussions.

---

## 1. Path mode — investigate any file

The flagship surface. Paste a file path (with optional line range) and get a
single-screen briefing assembled from local sources in under 5 seconds.

### Team routing
- **CODEOWNERS resolution** — wildcard-aware (`wcmatch`) lookup across the entire
  CODEOWNERS file with first-match precedence.
- **GitHub Teams enrichment** — team handle, member count, and the team's own
  description, fetched once per session and cached for 7 days.
- **Slack channel inference** — pulls the canonical alerts channel for the owning
  team via the Slack MCP, with manual overrides in `owners.json`.
- **On-call + escalation pointers** — surfaces the team's documented on-call
  rotation and escalation contact.
- **Manual override layer** — `owners.json` lets ops correct or augment any
  CODEOWNERS row without changing the source CODEOWNERS file.
- **Fallback ownership inference** — when CODEOWNERS doesn't match, the LLM
  examines top contributors and recent commit subjects to suggest a plausible
  owner with a confidence label.

### Contributors (DOK-lite ranking)
- **Degree-of-Knowledge scoring** — weights contributors by commit recency,
  commit count, and authorship of the original file, not just raw line ownership.
- **Active vs. departed flag** — cross-references a `departed.txt` list and the
  GitHub org roster so you don't try to ping someone who left the company.
- **GitHub username mapping** — maps each Alpaca engineer (by email or commit
  author) to their GitHub handle via the org roster cache.
- **First-author callout** — explicitly identifies who shipped the first commit
  to the file, often the truest "domain expert."

### PR + Jira context
- **Open PRs** — live `gh pr list` query against the file path, showing author,
  title, branch, and last-updated time.
- **Recently merged PRs (30d + 90d)** — two parallel `gh search` calls so you
  see both the immediate change history and longer-tail context.
- **Jira ticket extraction** — regex-pulls Jira IDs from commit subjects and PR
  titles, then fetches each ticket via `acli` to surface title, status,
  assignee, and a one-line summary. Cached 24 hours per ticket.

### Code context narrative (local LLM)
- **Locally-generated business intent** — `qwen2.5-coder:3b` running on Ollama
  produces a paragraph explaining *why* the code exists, grounded in commit
  messages + Jira ticket bodies, not file content guessing.
- **Decisions with evidence** — every claim in the narrative is backed by a
  pointer to a specific commit subject or Jira summary so you can verify it.
- **Anti-hallucination guardrails** — strict prompt rules prevent inventing
  team names, leadership claims, or generic "this file does X" filler.
- **Empty-state when offline** — if Ollama isn't running, the card explicitly
  shows "No local LLM active" with the exact `brew`/`ollama` commands to start
  it. No fake fallback content.
- **Active-model badge** — shows `local · qwen2.5-coder:3b` (or whatever model
  is configured) so the audience knows what's generating the text.

### Timeline + code preview
- **Vertical commit timeline** — recent commits to the file with author,
  subject, date, and a clickable link to the GitHub commit page.
- **Live blame on demand** — toggleable code snippet card with line numbers
  and per-line author attribution.
- **Hidden by default** — code preview lazy-loads on first "Show" click to
  keep the initial render light.

---

## 2. PR review mode

Paste a PR number and get a first-pass review checklist before you open GitHub.

- **Per-file aggregation** — runs a "lite" investigation (no LLM) on every file
  touched by the PR in parallel.
- **Reviewer ranking** — DOK-weighted list of who's most qualified to review,
  scored across the entire diff, not file-by-file.
- **Risk flags** — surfaces touched files in unowned directories, files with
  no recent contributors, and files where the PR author is the first-ever
  committer.
- **Review status tally** — shows the latest verdict per author (matching
  GitHub's merge-block rule, not raw event count).
- **Suggested next action** — "needs another approval," "blocked on changes
  requested by X," or "ready to merge."
- **File drill-down** — click any file in the PR to drop into full path-mode
  investigation for that file.

---

## 3. Contributors page

OrgLens-style directory of the entire engineering org.

- **Org-wide roster** — fetched once per week via GitHub GraphQL, covers every
  engineer in the org, not just CODEOWNERS members.
- **Team grouping** — filterable team chips with member counts.
- **View toggle** — switch between flat alphabetical and grouped-by-team views.
- **Activity badges** — green (active in last 30d), amber (90d), gray (older).
- **Last-active timestamps** — relative dates on every contributor card.
- **Per-contributor LLM summary** — lazy-loaded narrative paragraph that
  highlights each engineer's primary technical focus, derived deterministically
  from their top files and commit history (server-side `_PATH_DOMAIN_MAP`)
  before being polished by the local LLM.
- **Detail page** — full footprint per engineer: top files, top teams, recent
  commits, GitHub handle.

---

## 4. Snippet finder (floating bar)

A bottom-center pill that doubles as a global search.

- **OrgLens-style FAB** — always-visible 960px bar at the bottom of every
  screen, opens a paste modal on click or Enter.
- **`git grep` backend** — sub-second exact-match search across the entire
  repo, including untracked files.
- **Multi-line range expansion** — paste 5 lines, get the exact start/end line
  range in the matching file, not just the first hit.
- **Distinctiveness filter** — boilerplate (short comments, common imports)
  is filtered server-side so the results surface real code, not noise.
- **Click-through to investigation** — every result links to path mode with
  the file + range pre-filled.
- **Keyboard shortcuts** — `Enter` (idle page), `⇧⌘F` (anywhere), `Esc` to
  close, `⌘Enter` inside the modal to submit.

---

## 5. The non-negotiable: 100% local

Every byte of analysis happens on the machine running Codetective. No
proprietary code, commit messages, or Jira content ever leaves the laptop.

- **LLM**: Ollama (`qwen2.5-coder:3b` default; model-agnostic via env var).
- **Embeddings**: `nomic-embed-text` running locally via Ollama.
- **Vector DB**: SQLite file on disk (`/tmp/codemap-vectors.sqlite`).
- **Auth**: uses your existing `gh`, `acli`, and `git` credentials. No
  third-party API keys required.
- **Network egress**: only to GitHub (via `gh`), Jira (via `acli`), and Slack
  (via MCP) — same as your existing CLI tooling.

---

## 6. Performance + UX details

The stuff you don't notice until it's missing.

### Caching layers
| Cache | Backend | TTL | Purpose |
|---|---|---|---|
| `gh` API responses | disk (JSON) | 10m–1h | PRs, file blames, team lookups |
| LLM narratives | disk (JSON) | 1–24h | Repeat investigations are free |
| Jira tickets | disk (JSON) | 24h | Avoid rate-limited `acli` calls |
| GitHub org roster | disk (JSON) | 7d | Username-to-email mapping |
| Git shortlog | disk (JSON) | 24h | Org-wide commit counts |
| Contributor detail | disk (JSON) | 6h | Per-engineer top files |
| Contributor LLM summary | disk (JSON) | 24h | Lazy-loaded paragraphs |
| Path index | in-memory | session | Fuzzy autocomplete |
| Snippet search | in-memory | 60s | Repeat snippet-finder queries |
| Investigations | IndexedDB (browser) | session | Schema-versioned client cache |

### Concurrency + streaming
- **Parallelized backend** — git, GitHub, and Jira fetches run concurrently
  via `ThreadPoolExecutor`, not serially.
- **SSE progressive render** — `GET /investigate/stream` pushes each card
  (header, contributors, PRs, narrative, etc.) as it lands, instead of
  blocking on the slowest call.
- **Targeted DOM updates** — only the affected card repaints; the shell
  doesn't reflow.
- **Hover prefetch** — hovering a recent-investigation entry prefetches the
  payload so the click is instant.
- **Server prewarm** — a daemon thread on boot warms the `gh` cache, the
  Ollama LLM, the org roster, the team list, and the git shortlog so the
  first user query isn't a cold start.

### Frontend polish
- **Two-pane workspace** — persistent left rail (input, recent history) +
  tabbed right pane (everything else).
- **Deep-linkable URLs** — every investigation has a shareable URL with
  path + range encoded.
- **Repo switcher dropdown** — Alpaca-themed pill in the top-left, designed
  to support multiple repos when wired up.
- **Empty-state design** — gold-accented headline, three action cards
  (path / PR / contributors), and quick-try chips for instant demos.
- **Skeleton loading + flash-in animations** — every card shows a placeholder
  then fades in cleanly when its data arrives.
- **Alpaca brand theme** — gold/cream palette, typography, and the
  Codetective italic-gold logo treatment.

---

## 7. What's not built (yet)

Honest scope boundaries, useful for the roadmap discussion.

- **Multi-repo** — single repo at a time, configured via `GOBROKER_PATH`.
  Cross-repo would need a topology layer we deliberately didn't build in 6h.
- **Auth / multi-user** — single-user local server. No login, no tenancy.
- **CI integration** — no PR-comment bot, no GitHub Action. Manual paste only.
- **Cursor skill / MCP** — the "explain business intent" narrative would
  make a great Cursor skill but isn't packaged as one yet.
- **Rootly / Sentry routing** — escalation pointers come from CODEOWNERS +
  Slack, not from incident systems. Wiring Rootly + Sentry is the obvious
  next step for an on-call-first persona.

---

## Tech stack (one-pager)

- **Backend**: Python 3.11, FastAPI, Uvicorn
- **Frontend**: vanilla HTML/CSS/JS (no framework, no build step)
- **Data sources**: `git` CLI, `gh` CLI, `acli`, Slack MCP
- **LLM**: Ollama + `qwen2.5-coder:3b` (model-agnostic via env var)
- **Embeddings**: `nomic-embed-text`
- **Vector DB**: SQLite
- **Streaming**: Server-Sent Events
- **Client cache**: IndexedDB
- **Concurrency**: `concurrent.futures.ThreadPoolExecutor`
