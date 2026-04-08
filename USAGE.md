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
