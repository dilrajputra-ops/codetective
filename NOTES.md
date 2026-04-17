# Alpaca Codemap — Product Notes

> Dev/scope guardrails live in [AGENTS.md](AGENTS.md). This file is the product spec.

## Product Definition
Alpaca Codemap is an internal code-context tool for unfamiliar code.

Paste a code path, symbol, or line range and Codemap should show:
- the active engineering team working in that area
- the active contributor profiles around the path
- the timeline of change
- the related Jira ticket
- any open PR touching the code

## Main User Moment
I found unfamiliar code and need to know who owns it and the context behind it.

## Why This Matters
Shared Alpaca repos, especially `gobroker`, make ownership and intent hard to read quickly.

Engineers often need to answer:
- Which team is active here right now?
- Who recently changed this path?
- Is there already open work on it?
- What ticket or PR explains the current state?

## Core Outputs
1. `Active Eng Team working on this`
2. `Active Contributor Profiles`
3. `Open PR touching this code`
4. `Related Jira Ticket`
5. `Timeline of change`

## MVP Data Sources
- `CODEOWNERS`
- `git blame`
- `git log --follow`
- recent merged PRs touching the path
- open PRs touching the path
- Jira IDs extracted from PR title, branch, or metadata

## First-Screen UX
The first screen should feel like a calm case file.

Visible by default:
- path input
- one strong summary card
- active team
- contributor strip
- open PR / latest merged PR / related Jira
- compact timeline

Hidden behind disclosure:
- ownership nuance
- longer explanation of why the code exists
- deeper evidence

## Non-Goals For V1
- no chatbot
- no Slack search
- no Confluence search
- no contributor leaderboard
- no incident dashboard
- no org graph

## Demo Success Criteria
Within 30 seconds, an engineer should know:
- which team is active in the area
- which people recently worked on it
- what PR or Jira to open first
- whether there is work already in flight
