# Codetective

A small local tool that answers "who owns this code, who's actively touching it, and what's in flight against it?" Paste a path, get a single screen with the team, top contributors, recent commits, open PRs, and any related Jira ticket — in under three seconds.

Built for the on-call moment when you've been paged on code you've never seen and need to know who to escalate to before the first paragraph of the runbook.

## What it answers

| Question | Source |
|---|---|
| Which team owns this? | `.github/CODEOWNERS` (with parent-directory inference) |
| Who actually wrote this code? | `git blame -w -M -C` (whitespace + move-corrected) |
| Who's still on the team? | GitHub org membership via `gh` CLI |
| Who has likely left? | `departed.txt` (project-local) |
| Who knows it best, ranked? | DOK-lite scoring (see below) |
| What's in flight on it? | `gh api search/issues` for open PRs touching the path |
| Which Jira ticket explains the state? | Regex `\b[A-Z]{2,8}-\d+\b` over PR titles, branches, commit subjects/bodies |
| What does it look like in plain English? | One local Ollama call, JSON-mode, 5s timeout, templated fallback |

## What it deliberately does NOT do

- No cloud LLM, no cloud embeddings. **Nothing leaves the machine.** This is by design — the tool reads proprietary repo metadata (paths, author names, commit subjects, internal Jira IDs) and is meant to stay there.
- No chatbot, no NL Q&A. Paste a path, get a result. One screen, one input.
- No multi-repo. Single repo target via `GOBROKER_PATH`.
- No frontend framework. Single static `index.html` + vanilla JS + `fetch()`.
- No tests beyond a smoke script.

See [AGENTS.md](AGENTS.md) for the full scope guardrails.

## How the contributor ranking works (DOK-lite)

Adapted from Fritz et al. 2014 ([Degree-of-Knowledge: Modeling a developer's knowledge of code](https://dl.acm.org/doi/10.1145/2512207)), minus the IDE-interaction term we don't have signal for:

```
score(person) =
    blame_share        * 1.0    # primary signal (whitespace/move-corrected)
  + recency_term       * 1.5    # 6-month half-life, capped at 3.0
  + authorship_bonus   * 0.8    # +1 if first author of the file
  + change_volume      * 0.5    # log10(1 + non-trivial lines added)
  - departed_penalty   * 5.0    # effectively zeroes out departed contributors
```

All weights are tunable constants at the top of [server/expertise.py](server/expertise.py). The `score_breakdown` is returned alongside each contributor so the UI can show "why this person ranked here" on hover — no magic.

The role label is layered on top: if `employees.status() == "departed"` the label becomes "Left Alpaca"; if the person is a current member of the owning team (matched via GitHub login, with fallback to the `noreply` email pattern) the label becomes "Current <Team> member"; otherwise the DOK-derived label ("Created the file" / "Active contributor" / "Historical author") wins.

## Architecture

```
                  +------------+
   path  ---->    |  /         |  --> serves index.html
                  |  /file     |  --> raw file content for the visual range picker
                  |  /paths    |  --> autocomplete list (cached git ls-files)
                  |  /investigate -> the case dict the UI renders
                  |  /reindex  |  --> rebuilds the path vector index
                  |  /health   |
                  +-----+------+
                        | (FastAPI on :8765)
        +---------------+----------------+--------------+--------------+
        |               |                |              |              |
   git_ops          codeowners      gh_client      expertise       llm
   (blame/log/      (CODEOWNERS    (open PRs,    (DOK-lite      (Ollama,
    stats/first    parser, parent  on-disk        contributor     5s timeout,
    author)        inference)     cached)         scoring)        JSON mode,
                                                                   templated
                                                                   fallback)
        |
   employees + owners_map + gh_teams + slack_lookup
   (HR/team membership, slack channel routing,
    departed lookup with mtime-cached departed.txt)
```

Each module is small (<150 lines). All shellouts have explicit timeouts and never use `shell=True`. All git/gh paths must resolve under `GOBROKER_PATH` or the request gets a 400/404.

## Setup

Requires Python 3.13+ and macOS or Linux.

### 1. Install local services

```bash
brew install --cask ollama
brew install gh
ollama pull llama3.2:3b nomic-embed-text
gh auth login            # needs `repo` and `read:org` scopes
```

### 2. Clone the target repo

Anywhere on disk. Just point at it via `GOBROKER_PATH` in `.env`.

### 3. Install Python deps

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 4. Configure

Copy `.env.example` to `.env` and fill in:

```
GOBROKER_PATH=/path/to/your/repo/clone
GH_REPO=alpacahq/gobroker
GH_ORG=alpacahq
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=llama3.2:3b
OLLAMA_EMBED_MODEL=nomic-embed-text
VECTOR_DB=/tmp/codemap-vectors.sqlite
```

### 5. Optional: project-local data

- [departed.txt](departed.txt) — one substring per line that matches a departed contributor's name or email. Case-insensitive. Lines starting with `#` are ignored.
- [owners.json](owners.json) — CODEOWNERS team slug → routing info (slack, on-call, escalation, docs). Fields are all optional; missing ones just don't render.
- [slack_channels.json](slack_channels.json) — generated cache of CODEOWNERS slug → Slack channel mapping.

## Run

```bash
source venv/bin/activate
uvicorn server.main:app --host 127.0.0.1 --port 8765 --reload
```

Open http://127.0.0.1:8765 and paste a repo-relative path.

## Smoke test

After any change to the contributor scoring or git plumbing:

```bash
python scripts/smoke_expertise.py path1 path2 path3
# or, no args = auto-pick 3 random paths from the target repo
python scripts/smoke_expertise.py
```

Prints contributors with score breakdown, sanity-checks ordering and departed handling.

## Privacy posture (read this if you're considering deploying it for someone else)

Codetective reads:
- file paths from a private repo
- author names and email addresses from git blame and git log
- commit subjects and bodies
- PR titles, branches, and authors via the GitHub API (under your `gh` auth)
- Jira IDs extracted from the above

It sends:
- nothing to any cloud service
- one local HTTP request per investigation to `OLLAMA_HOST` (default `localhost:11434`) with the above signals as JSON

If you swap the Ollama host for a hosted endpoint, you've defeated the design. There is no API-key auth in this project on purpose — there's nothing to authenticate to.

## Repo layout

```
server/
  main.py            # FastAPI app, route definitions
  config.py          # env vars, paths
  synth.py           # combines all signals into the CASE dict the UI renders
  git_ops.py         # blame, log, log --numstat, first_author, file IO
  codeowners.py      # CODEOWNERS parser + parent-directory inference
  gh_client.py       # open PR lookup via `gh` CLI, on-disk caching
  expertise.py       # DOK-lite contributor scoring
  employees.py       # mtime-cached departed.txt lookup
  owners_map.py      # CODEOWNERS slug -> routing dict (slack, on-call, etc.)
  gh_teams.py        # GitHub org team membership fetcher (cached)
  slack_lookup.py    # slack channel resolution helpers
  jira_extract.py    # regex extraction + dedup of Jira IDs
  llm.py             # Ollama call, JSON mode, templated fallback
  vectors.py         # SQLite path-embedding cache + brute-force cosine
  paths_index.py     # cached `git ls-files` for autocomplete

index.html           # single-file UI (vanilla JS, no build step)
scripts/
  smoke_expertise.py # contributor ranking smoke test

owners.json          # team routing
departed.txt         # known-departed contributors
slack_channels.json  # team slug -> slack channel cache
```

## License

Private project. No license granted. Don't deploy this against a repo you don't have read access to.
