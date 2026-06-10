# Drive a real Jira ticket through the rebuilt console — step-by-step

Worktree: `~/code/citemed/ranch-newconsole` (isolated from live ranch).

This is the operator-facing recipe for taking a single Jira ticket from
the ranch hand picking it up, through the propose/execute loop, to a
merged PR. Steps marked **operator** require human action; the rest are
automatic once the loop runs.

---

## Prereqs (one-time)

### 1. Jira auth
- `RANCH_JIRA_API_TOKEN` set to a token from
  https://id.atlassian.com/manage-profile/security/api-tokens
- `.ranch-dev/config.toml` already has the `[jira]` section pointed at
  `ethan@citemed.io` (the ranch-hand account for now). Edit if you want
  a different account.

### 2. Bitbucket auth (for PR open/comment)
- `bb auth status` shows green. If not, `bb auth login` (Atlassian) or
  set `BITBUCKET_REPO_TOKEN` per `~/.claude/CLAUDE.md`.

### 3. Real worktree path
- `.ranch-dev/config.toml`'s `[agents.max].worktree` currently points at
  the fake worktree under `.ranch-dev/fake-worktree`. For a real drive,
  edit that to a real citemed_web worktree (e.g. one of `~/code/citemed/{max,jeffy,arnold,kesha}`)
  so the agent has real code to edit.
- Note: the live ranch points at `~/.ranch/` and is unaffected by this
  config file.

---

## Step 1 — Create + label a Jira ticket (operator)

In Jira:
1. Create a small, low-risk ticket. Suggested first drive: README typo
   fix, missing test, or a renamer. Keep scope so small the loop runs in
   one cup of coffee.
2. **Assignee:** `ethan@citemed.io` (matches `[jira].hand_account`).
3. **Labels:** add `ranch-max` (or whichever hand you want to drive).
4. Status: To Do.

## Step 2 — Confirm routing query sees the ticket (operator)

```sh
cd ~/code/citemed/ranch-newconsole
source .venv/bin/activate
RANCH_HOME="$(pwd)/.ranch-dev" \
  RANCH_DATABASE_URL="sqlite:///$(pwd)/.ranch-dev/ranch.db" \
  ranch triage --agent max
```

Expected: your new ticket appears in the table, scoped to
`ranch-max` routing. If it doesn't:
- Re-check label spelling (`ranch-max`, lowercase, no `ranch-initiative:`)
- Re-check assignee email matches `[jira].hand_account` in dev config
- Run `ranch triage --agent max --all` to see all your assigned tickets
  unscoped — confirms Jira auth is healthy.

## Step 3 — Start the hand (will run continuously)

```sh
# Same env exports as above
ranch hand start max --poll 10
```

You should see:
```
ranch hand 'max' started  (cwd=..., poll=10.0s)
max: no active work — triaging...
max: picked ECD-XXXX
```

Within a poll cycle the hand spawns a propose orchestrator that:
- Reads the ticket via `mcp__atlassian__getJiraIssue`
- Drafts a plan
- Calls `record_state` + `record_checkpoint("plan_ready")`
- Parks

## Step 4 — Watch the kanban (operator)

In another terminal:

```sh
cd ~/code/citemed/ranch-newconsole
# sidecar already up if you've been running `./scripts/dev.sh`
./scripts/dev.sh    # if not — starts vite renderer + Electron
```

Open the dev console window. Switch to the `max` tab; the new ticket
should appear in the `plan` column with ⚠ + `plan_ready` indicator.
Click the card → side panel shows Goal / Done / Now / DECIDE with the
real plan content.

## Step 5 — Approve plan (operator, via UI)

Click **Approve** in the side panel.

What happens behind the scenes:
- POST `/api/runs/{id}/approve` queues an `Interjection(kind=approve)`
- Hand's main loop picks it up via `_find_approved_parked_propose`
- Spawns execute orchestrator carrying the plan + acceptance contract
- Card moves to `code` within one SSE tick

## Step 6 — Watch execute progress (no operator action needed)

The agent works through the plan. Cards transition `code → verify →
pre_push` automatically as the agent calls `record_state`. The side
panel's "Now" line updates live. The expand-pane (click a DONE step)
shows the per-step `details` the agent recorded.

## Step 7 — Approve pre_push (operator, via UI)

When the card lands at `pre_push`:
- DECIDE section shows real diff stats (`+N/-M` per file)
- `recommended_action` is shown ("DEPLOY recommended" / "NO DEPLOY needed")

Click **Approve**. The agent receives the approval message (which
includes branch/commit/push instructions baked into prompts.py), runs:
- `git fetch origin develop`
- `git checkout -B <ticket>-fix origin/develop`
- Stages + commits with proper message
- `git push -u origin <branch>`
- `bb pr create -t "..." -b "..."`

`Run.pr_id` and `Run.pr_url` get populated. Card transitions to
`pr_open`.

## Step 8 — Observe CI + review polling

The hand's `ci_loop` polls Bitbucket pipelines every 60s by default and
emits `ci_flip` events. The `pr_loop` polls for new review comments
every 120s. Both surface in the activity log:
- `⚡ CI passed on PR #1234`
- `💬 1 new comment from <reviewer>`

When a comment arrives:
- Card transitions `pr_open → review`
- Side panel shows `comments_preview` and `decide_kind=respond_to_review`

## Step 9 — Approve respond-PR triage (operator, via UI)

The respond-PR orchestrator drafts a triage table:
- For each comment: file:line, validity (AGREE/DISAGREE), proposed action

Click **Approve** on the triage. Agent applies fixes, posts inline
replies via `bb pr comment`, marks comments resolved.

## Step 10 — Merge

When the PR is merged (manually in Bitbucket, or via `bb pr merge` from
CLI), the next PR-poll tick detects `state=MERGED`, sets `Run.state =
"merged"`, and the card moves to the `merge` column. Any tickets that
were blocked by this one (P1's `record_block`) auto-resolve.

---

## Troubleshooting

**Card doesn't move within SSE tick:** sidecar may have stale view-model.
Hard-refresh the UI (cmd-R). If still stale, restart sidecar:
```sh
lsof -nP -iTCP:8421 -sTCP:LISTEN -t | xargs kill
RANCH_HOME="$(pwd)/.ranch-dev" \
  RANCH_DATABASE_URL="sqlite:///$(pwd)/.ranch-dev/ranch.db" \
  ranch serve &
```

**Approve queues but execute doesn't fire:** the propose Run might have
an unresolved block. Check:
```sh
ranch view-hand max --json | jq '.tickets[] | select(.blocked_by)'
```
If blocked, approve the blocker first or `ranch unblock <run_id>`.

**Hand stuck at "no viable tickets":** routing label / assignee mismatch.
Re-run step 2 to confirm the triage query returns your ticket.

**Live ranch session impact:** none. The dev hand reads
`.ranch-dev/ranch.db` (via `RANCH_DATABASE_URL`) and writes `userData`
to `~/Library/Application Support/ranch-console-dev/`. The live ranch
points at `~/.ranch/` and a different userData dir.

## Reset between drives

```sh
cd ~/code/citemed/ranch-newconsole
# Stop the hand
ranch hand stop max
# Stop the sidecar
lsof -nP -iTCP:8421 -sTCP:LISTEN -t | xargs kill
# Re-seed if you want demo data back
RANCH_DATABASE_URL="sqlite:///$(pwd)/.ranch-dev/ranch.db" \
  .venv/bin/python scripts/seed_demo.py
```
