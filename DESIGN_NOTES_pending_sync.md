# Design notes — pending sync to issue #110

These are design locks made in conversation that need to land on the
tracking issue (https://github.com/ethandrower/ranch/issues/110) once
gh auth resettles. Capture them here so we don't lose them.

## A.5 — Demand-driven backlog discovery (locks)

**Q1 — operator-gated for ALL tiers.** No auto-relabel even on Tier 1.
Every proposed takeover lands as a queued recommendation the operator
approves. Each card carries:

- Tier badge (T1 inbox / T2 stale / T3 stalled)
- Confidence score (does this touch this hand's code paths?)
- One-line case for taking it
- Risk flags if any

**Q2 — Jira comment as the dedupe lock.** When a hand spots a candidate,
it posts a structured proposal comment on the Jira ticket:

    [ranch-claim:max:pending @ 2026-06-10T14:32Z]
    reasons: easy / no deps / behind 12d / touches ref-mgmt

Other hands' discovery routine parses ticket comments first; any
existing `[ranch-claim:*:pending]` causes them to skip. Operator
approval edits the comment to `:approved` and adds the routing label.
Rejection edits to `:rejected` and the ticket re-opens for next
discovery tick. Stale `:pending` older than 24h is treated as
abandoned (crashed proposing hand).

**Q3 — no separate comment on takeover.** The proposal comment IS the
announcement. Approve/reject just mutates it. Operator can opt in to a
courtesy comment to the original assignee via a checkbox in the
approval dialog.

## Park-and-resume (architectural)

- Always park when waiting for external state (CI / deploy / merge /
  review). Drop the inline-sleep-under-30s heuristic.
- Cold-start cost on resume is acceptable; user explicitly does not
  care to optimize for it.
- Polling lives in the hand orchestrator's asyncio loop, not in the
  agent SDK session. Hand polls; on flip, hand resumes the SDK with
  `resume=<session_id>` and feeds it the result.
