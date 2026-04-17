---
description: Poll a ranch run's PR for review comments on a cadence and surface new ones for triage
argument-hint: <run_id> [interval]
---

You are watching ranch run **$1** for new PR review comments.

Default cadence: **10 minutes**. Override by passing a second argument, e.g. `/ranch-watch-pr 7 5m`.

## What to do on each tick

1. Run `ranch poll-pr $1` once.
2. If the output contains `no new comments`, stay silent — do not echo anything to the user. Schedule the next tick via the loop skill.
3. If the output lists new comment(s):
   - Summarize: who commented, how many, one-line preview of each.
   - Recommend the next step: "Run `ranch respond-pr $1` to start the triage → fix workflow."
   - Stop the loop (the user will decide when to respond).

## On errors

If `ranch poll-pr` fails (PR not found, bb/gh auth expired, etc.), surface the error clearly and stop the loop. Do not retry indefinitely.

## Kickoff

Invoke this with `/loop ${2:-10m} ranch poll-pr $1`. The loop runs `ranch poll-pr $1` on the cadence; your role is to read each tick's output and react per the rules above.
