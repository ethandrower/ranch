# Ranch — Quick Reference

## Start a run

```bash
# Standard ticket (plan → TDD → QA → pre-push)
ranch run max --ticket ECD-123 --brief "Add /healthz endpoint"

# Brief from a file
ranch run max --ticket ECD-123 --brief ~/briefs/ecd-123.md

# Free-form task — no enforced workflow (PR review, bug investigation, etc.)
ranch run max --ticket ECD-123 --free \
  --brief "PR is open at #89. Review comments, reply, make changes where reviewer is correct."
```

## Mid-run commands (type these while the agent is running)

```
!approve                    Resume after a checkpoint — agent continues
!reject must include auth   Resume with rejection reason — agent adjusts
!note also handle 429s      Forward a note mid-run, agent reads it immediately
!stop                       Clean exit, saves state
```

## Out-of-process commands (any shell, by run_id)

These work for both foreground runs and dispatched (background) runs.

```bash
ranch approve 42                    # release a checkpoint
ranch approve 42 --note "LGTM"
ranch reject 42 "scope too wide"
ranch note 42 please handle 429s    # forward a note
ranch stop 42                       # clean exit
```

## Background runs (dispatch)

```bash
ranch dispatch max --ticket ECD-123 --brief "Add /healthz endpoint"
# → Dispatched run #7 (max / ECD-123)
#     PID:  88421
#     Log:  /Users/you/.ranch/logs/run_7.log
#     Approve with: ranch approve 7

ranch status                        # table of all active runs
ranch status 7                      # detail for one run (PID liveness, pending checkpoint)
ranch watch --timeout 30            # block until any run transitions, silent on timeout
ranch watch --run 7                 # block on one specific run
tail -f $(ranch log 7)              # stream the run's log
```

`ranch watch` is designed for `/loop`:

```
/loop 30s ranch watch --timeout 30
```

## PR review feedback loop

After a run pushes a PR, poll for reviewer comments and respond.

```bash
ranch poll-pr 7                      # auto-discovers PR from branch, fetches new comments
ranch poll-pr 7 --pr 367             # manual PR attachment
ranch respond-pr 7                   # resumes the agent with TRIAGE → FIX → PRE-PUSH workflow
ranch resolve-comment 7 <cid> --sha <sha>   # usually called by the agent after each fix commit
```

Continuous polling via `/loop`:

```
/loop 10m ranch poll-pr 7
```

`poll-pr` is designed to be loop-friendly: quiet when there's nothing new,
loud (lists comments + suggests `ranch respond-pr`) when fresh comments arrive.
See `.claude/commands/ranch-watch-pr.md` for a slash-command wrapper.

## Checkpoints (agent-initiated pauses)

The agent calls these itself. You respond with `!approve` or `!reject`.

| Checkpoint     | Requires approval? | When                          |
|----------------|--------------------|-------------------------------|
| `plan_ready`   | yes                | After planning, before coding |
| `tests_green`  | no                 | Tests pass, continues to QA   |
| `pre_push`     | yes                | Before any push or PR         |
| `custom`       | no                 | Agent-discretion (free mode)  |

## Manage runs

```bash
ranch runs                  # list all runs
ranch runs --agent max      # filter by agent
ranch resume 3              # resume run #3 by SDK session ID
```

## Ranch hand daemon (Phase H11 — MVP)

The persistent virtual engineer that runs the pilot loop on autopilot.
Picks the next viable Jira ticket, gathers context, drafts a plan, parks
for your review.

```bash
ranch hand start max --poll 30 --project ECD    # foreground daemon for agent 'max'
ranch hand status                               # see what every hand is doing
ranch hand stop max                             # graceful stop after the current step
```

MVP scope: one ticket at a time per hand. The hand triages → scopes →
proposes → parks. While a recently-parked proposal is awaiting your
review (24h window), the hand idles instead of piling on new work.

After you review a parked proposal:
- If you want to proceed, run `ranch run max --ticket <key> --brief "<plan>"`
  using the plan from the dossier — execution wiring lands when the next
  H-tickets layer in (H8 self-judge, H9 labs handoff, H10 PR draft).
- If you want to reject, just delete or supersede the parked run; the
  hand will triage past it once it falls outside the 24h window.

Stop semantics: `ranch hand stop` writes a sentinel file the daemon polls
for between cycles — clean exit, never kills mid-tool-use.

## Propose a plan (Phase H6)

Run a bounded, file-system-read-only SDK session that produces a plan +
acceptance criteria for a ticket and parks for approval. Consumes the
scope bundle saved by `ranch scope --save`.

```bash
ranch scope ECD-1234 --save                          # produces ~/.ranch/scopes/ECD-1234.md
ranch propose ECD-1234 --agent max                   # bounded plan session
ranch propose ECD-1234 --cwd /path/to/worktree       # explicit worktree
ranch propose ECD-1234 --agent max --budget 300      # extend the 180s default

ranch dossier <run_id>                               # inspect the proposal afterwards
```

Hard guarantees:
- The agent has no Write or Edit tools during propose — your worktree
  cannot be modified, branches cannot be created.
- Bash is allowed but only sensibly used for read-only commands; the
  system prompt instructs the agent not to mutate state.
- A wall-clock budget (default 180s) bounds cost; on overrun the
  orchestrator requests a clean stop.

The final dossier is `state=parked` with the full plan + acceptance
criteria in `details` (Confluence-expand long-form content), and
options=`approve|reject`. Approving flows the plan into a full
`ranch run` (H6 → H8 wiring lands with H11 ranch hand).

## Scope a ticket (Phase H5)

Build a pre-flight context bundle for a ticket — epic, sister tickets, open PRs, design links, Confluence refs. The ranch hand calls this before planning so the agent starts with the full picture instead of discovering it tool-call by tool-call.

```bash
ranch scope ECD-1234                 # print bundle to stdout (markdown)
ranch scope ECD-1234 --save          # also persist to ~/.ranch/scopes/ECD-1234.md
ranch scope ECD-1234 --json          # JSON for the ranch hand scheduler
ranch scope ECD-1234 --cwd /path/to/worktree  # use a specific repo for `bb pr list`
```

The bundle includes:
- Ticket itself: status, priority, assignee, labels, full description
- Parent epic (if any), plus all sister tickets in the same epic
- Open Bitbucket PRs whose branch or title references this ticket, any sister, or the epic
- All figma.com URLs found in the ticket or epic
- All Confluence wiki URLs found in the ticket or epic

PR discovery is best-effort via `bb` — if `bb` is missing or auth fails, that section is empty rather than aborting the bundle.

## Triage assigned Jira tickets (Phase H4)

Rank your assigned Jira tickets by viability (status, design presence, AC clarity, priority, age) so the ranch hand (or you) can pick what to work on next.

```bash
ranch triage                         # top 10 viable tickets across all projects
ranch triage --project ECD           # filter to one Jira project
ranch triage --agent max --top 5     # exclude tickets max is already in flight on
ranch triage --json                  # machine-readable, consumed by the ranch hand scheduler
```

One-time setup in `~/.ranch/config.toml`:

```toml
[jira]
url = "https://yourorg.atlassian.net"
email = "you@example.com"
```

Then export your API token (create at https://id.atlassian.com/manage-profile/security/api-tokens):

```bash
export RANCH_JIRA_API_TOKEN="…"
```

Scoring axes (higher is more viable):
- **Status**: +30 in-progress, +20 to-do, 0 if blocked / waiting-for-design / on-hold
- **Design link**: +20 if a figma.com URL is in the description or comments
- **AC clarity**: +15 if "Acceptance Criteria" header or numbered should/must items
- **Priority**: Highest +15, High +10, Medium +5, Low 0, Lowest -5
- **Age**: log-bounded, max +10 (older tickets get a small boost so nothing rots)
- **In-flight penalty**: -1000 (excluded) if this agent already has a non-terminal run on the ticket

## Dossier view (agent self-report — Phase H)

While a run is happening, the agent emits structured dossier updates
(plan progress, what it just did, current phase, blocker if parked).
View them live without scrolling the transcript:

```bash
ranch dossier 3                  # latest dossier for run #3
ranch dossier 3 --watch          # repaint live as the agent emits updates
ranch dossier 3 --json           # raw JSON for scripting

ranch fleet                      # all active runs at a glance
ranch fleet --watch              # live-refreshing fleet view
ranch fleet --all                # include completed/stopped runs
```

`ranch dossier --watch` auto-exits when the run reaches a terminal state
(completed / stopped / error). Ctrl-C exits early.

## Memory

```bash
ranch feedback              # recent captured corrections (last 20)
ranch feedback --limit 50

ranch lessons               # all active lessons by confidence
ranch lessons --category django_specific

ranch reflect               # run reflection on current git branch's ticket
ranch reflect ECD-123       # run reflection on a specific ticket
```

## Context injection (paste into a new CC session)

```bash
ranch context                        # all applicable lessons
ranch context --tags django,api      # filtered by tag
ranch context --out /tmp/ctx.md      # write to file, then paste into CC
```

## Fleet status

```bash
ranch status    # agents, active tickets, memory counts
ranch init      # (re)init DB + verify worktrees — safe to re-run
```

## Agents

Configured in `~/.ranch/config.toml`. Edit to add/remove worktrees.

```toml
[agents.max]
worktree = "/Users/ethand320/code/citemed/max"

[agents.jeffy]
worktree = "/Users/ethand320/code/citemed/jeffy"

[agents.arnold]
worktree = "/Users/ethand320/code/citemed/arnold"
```

## Data files (`~/.ranch/`)

| File                  | Contents                              |
|-----------------------|---------------------------------------|
| `ranch.db`            | All tickets, feedback, lessons, runs  |
| `config.toml`         | Agent registry                        |
| `active_tickets.json` | Session → ticket state (hooks)        |
| `reflection.log`      | Async reflection output               |
| `hook_errors.log`     | Hook errors — check here if silent    |
