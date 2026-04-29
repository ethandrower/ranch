# Ranch Roadmap

Ranch is a **local-first console for Claude Code agent fleets**. It does not replace the interactive Claude Code session — it sits around it, managing the things Claude Code itself will never own: which worktrees exist, which agent has which ticket, which docker stack is on which port, which PRs are waiting on review feedback, and which lessons should be injected when a session starts.

The product is not a CLI runner that streams JSONL. The product is the **console** — an Electron app — and the supporting layers that make it possible to operate N agents across M repositories without cognitive overload.

---

## What ranch is, in one paragraph

The interactive CC tab is the unit of work. Plan mode, hooks, slash commands, approval — those stay where they are. Ranch is the layer above: a project + agent registry, a port ledger, a docker orchestrator, an event bus, a memory layer, and a UI that ties them together. Every CC session — whether interactive (human in the loop) or autonomous (SDK + auto-approve overnight) — emits events to the same bus and is rendered in the same grid. You open ranch, pick the repos you care about, spin up N agent sessions, and see at a glance: who's working on what, which docker stack is up on which port, which PR has new review comments, what's awaiting approval, and where each session left off.

---

## Status

**Phases 0 through 2+ shipped.** Memory capture, lesson reflection, single-agent SDK orchestrator with checkpoints, mid-run interjections, free-form mode, hook-based approval delivery, `--auto-approve`, `bb` CLI integration. 58 tests passing.

**Phases 3a/3b/5a/5b shipped (commit dd59e28).** Background dispatch (`ranch dispatch`), fleet watch (`ranch watch`), out-of-process interjection (`ranch approve/reject/note/stop <run_id>`), PR feedback poll/respond loop (`ranch poll-pr`, `ranch respond-pr`).

**Phase A1 in review (PR #35).** Electron + Vite + React skeleton with typed IPC surface and config loader landed.

The CLI works. The next phase is the **console** — an Electron app that exposes all of this through a UI you can actually live in.

---

## Sequencing — MVP first, full vision second

Everything described below is still wanted. The **end state** is the full architecture: project registry, port ledger, docker orchestration, autonomous dispatch, inbox, memory panel. The sequencing question is which slice delivers value first.

The **immediate-value slice** is not "scale ranch to N projects and M agents." It is **"give me ambient awareness of the four worktrees I'm already developing in."** Today the operator opens four iTerm tabs, runs `claude` in each, and loses track within an hour of which tab is on which ticket, what each one's TODO list looks like, and which port to hit when testing. That problem is solved by a single rich screen — no project registry, no port ledger, no docker orchestration, no autonomous mode required. The four worktrees and their compose stacks already exist; ranch just needs to **observe and surface** what's happening in them.

So the roadmap is split:

- **Phase MVP** — Ambient awareness over the existing hardcoded four-worktree setup. Read-only observation, embedded terminal attach, todo/branch/port visibility. **This is the next thing built.**
- **Phases A (remainder), B, C, D, E, F** — The full vision. Sequenced after MVP proves the UX is right.

Phase B (`ranch.project.toml`, port ledger, workspace lifecycle), Phase C (docker orchestration), Phase D (autonomous mode), and parts of Phase A (project registry, dispatch UI) are deliberately deferred. They are still in scope long-term — they unlock multi-project use, fifth-agent-without-code-change, and overnight unattended runs — but none of them are required to fix the day-one context-switching pain.

---

## Architecture (the pivot)

### Two channels, never crossed

1. **Display channel** — pty bytes from a real terminal into `xterm.js` in the renderer. For interactive sessions only. Unparsed. The human's eyeballs are the consumer. No semantic extraction from this stream — that's a tar pit.
2. **Event channel** — structured `RunEvent` envelopes flowing from CC hooks (interactive mode) and the SDK runner (autonomous mode) into a single event bus. The bus persists to the DB and fans out to subscribers (the console renderer, the inbox, notification routers). Every UI element above "raw terminal" reads from this channel.

### Two dispatch backends behind one schema

| | Interactive | Autonomous |
|---|---|---|
| Driver | `node-pty` running `claude` CLI under `tmux` | SDK session in worker subprocess |
| Approvals | Human via plan mode | `--auto-approve` by default |
| Output | xterm.js, focus-driven | log file, tail-on-demand |
| Lifecycle | As long as session is open | Task-driven; ends when SDK ends or budget hits |
| Detach | Closing console keeps tmux session alive | Worker keeps running, supervised |
| Use case | Intensive build, Q&A, debugging | Overnight build-fix, bulk PR triage |

Both write the same `Run`, `Checkpoint`, `Decision`, and `Lesson` rows. Two adapters, one schema, one event stream. Don't unify them under a single runner with mode flags — the duty cycles and failure modes are different enough that abstraction-fitting will hurt.

### Project config lives in ranch, not in repos

Today citemed_web's Makefile owns the agent roster, the port table, and the worktree base. That's wrong — those are operator concerns, not application concerns. They block reuse across repos and make adding a fifth agent a code change.

Ranch becomes the source of truth for:
- The agent registry (`~/.ranch/config.toml`)
- The project registry (paths to repos, their compose fragments, their per-agent service definitions)
- The port ledger (per-(project, agent) port assignments, persisted in the DB)
- Workspace lifecycle (create / destroy / reset / sync-env)
- Shared infra lifecycle (postgres + redis + docker network)

Each application repo provides only:
- `docker-compose.agent.yml` — per-agent service definitions
- `docker-compose.shared.yml` — shared service definitions
- `.env.agent.template` — variables ranch will fill in
- `ranch.project.toml` — declares the above + project metadata

The Makefile loses its agent block entirely.

---

## What we learned (Phases 1–2+)

- **Hook-based approval delivery.** PostToolUse hook awaits the human decision and returns it as `additionalContext`. Eliminates the race between SDK system notifications and the approval payload.
- **`append_system_prompt` for CLAUDE.md.** Each worktree's CLAUDE.md is appended via the SDK parameter rather than injected into the brief.
- **Pydantic message contracts.** `CheckpointInput`, `HumanDecision`, `HumanNote` validate everything flowing between orchestrator, hooks, stdin, and DB.
- **Branch-off-develop enforcement** via worktree CLAUDE.md prevents accidental main-branch pushes.
- **DB-polled interjection table** unified the foreground stdin loop and out-of-process CLI commands behind one code path.

These all carry forward. The console builds on top of them; it doesn't replace them.

---

## Done

### Phase 0: Foundation
SQLAlchemy + SQLite schema, agent registry, Click CLI scaffold, `ranch init`, `ranch status`.

### Phase 1: Memory capture
`UserPromptSubmit` and `SessionEnd` hooks → episodic feedback → reflection → semantic lessons → `ranch context` injection. Ticket ID extraction from branch names.

### Phase 2: Checkpointed orchestrator
`ranch run` with structured workflow (plan_ready, tests_green, pre_push), streaming output, mid-run interjections, `ranch resume`, `ranch runs`, `--free` mode.

### Phase 2+: Hardening
Rate-limit retry, Pydantic contracts, hook-based approval, `--auto-approve`, daemon-thread stdin, `append_system_prompt` for CLAUDE.md, test DB isolation, `bb` CLI integration.

### Phase 3a/3b: Background dispatch + watch
`ranch dispatch` runs in the background and returns immediately. `ranch watch` blocks until any agent completes. Process tracking + log path in the Run record.

### Phase 5a/5b: PR feedback loop
`ranch poll-pr <run_id>` (loop-friendly, idempotent) fetches review comments. `ranch respond-pr <run_id>` resumes the agent with a triage → propose → push checkpoint structure.

### Phase 11 (out-of-band): Out-of-process interjection
DB-polled `Interjection` table. `ranch approve/reject/note/stop <run_id>` work from any shell against any background run.

---

## Phase MVP — Ambient awareness of the existing four worktrees

**Goal:** open ranch, see all four worktrees on one screen, know what each one is doing without context-switching, attach to any of them in an embedded terminal.

**Hard constraint:** no new configuration. Hardcoded list of four worktrees (`max`, `jeffy`, `arnold`, `kesha`) at known paths. Ports read from each worktree's existing `.env.agent` file (created by citemed_web's current `make init-agent`). Compose stacks remain managed via `make` as today — ranch only **displays** stack state, it doesn't bring stacks up or down.

### What each card on the grid shows

For each of the four worktrees:

1. **Identity** — agent name, worktree path
2. **Branch state** — current branch, dirty/clean, commits ahead/behind `origin/develop`, last commit (sha + message + age)
3. **Ticket** — derived from branch (`ECD-1234` from `feature/ECD-1234-foo`)
4. **Topic** — auto-derived from latest user prompt or first non-empty TodoWrite item; editable inline
5. **Live TODO state** — current TodoWrite list parsed from the active CC session transcript: completed / in-progress / pending counts plus the in-progress item's text
6. **Last activity** — timestamp of last assistant message or tool call (`active 2m ago`, `idle 14m`)
7. **CC session presence** — is `claude` running in a tmux session for this worktree? (running / detached / none)
8. **Local ports** — `DJANGO_PORT` and `VITE_PORT` from `.env.agent`, with click-to-open-in-browser
9. **Open PR** — branch's PR number + status if one exists (use existing `bb` integration)

### What clicking a card does

- Embedded terminal pane opens
- Attaches to the agent's tmux session (`tmux new-session -A -s ranch-<agent>`) — if no session, creates one and runs `claude` inside it
- Closing the pane detaches; closing the window keeps the tmux session alive

### Issues

- **MVP-1.** Hardcoded four-worktree config bridge — read each worktree's `.env.agent` for ports + read `~/.ranch/config.toml` for paths
- **MVP-2.** Git state observer — branch, dirty, ahead/behind, last commit per worktree (polled, ~5s)
- **MVP-3.** CC session transcript parser — find the active session JSONL for a worktree (already partially solved by `ranch/transcript.py`), extract latest TodoWrite state + last activity timestamp
- **MVP-4.** CC process detection — is there a `claude` process attached to this worktree's tmux session?
- **MVP-5.** Rich worktree grid card — pull the above together into the card UI (replaces the read-only A4 stub)
- **MVP-6.** Embedded terminal attach via tmux — minimal subset of A6: `tmux new-session -A` against an existing session, no full PTY orchestration yet
- **MVP-7.** Click-to-open-in-browser using ports from `.env.agent`

### Explicit non-goals for MVP

- No project registry (single hardcoded project)
- No `ranch.project.toml` (no externalization)
- No port allocation (ports already exist in `.env.agent`)
- No docker compose lifecycle from the console (use `make` as today)
- No autonomous dispatch
- No "New session" modal — sessions are launched the way they are today (open terminal, run `claude`); ranch attaches to whatever's there
- No event bus daemon — for MVP, the renderer polls the file system + git + transcript on a low cadence. The bus comes back when we move beyond polling.
- No inbox — checkpoint-awaiting / PR-comment notifications come later

### Acceptance for Phase MVP

- Open ranch after lunch, see all four worktrees with current branch, current TODO, last activity, port, and CC session state
- Click `max` → terminal attaches to max's tmux session running `claude`; close the pane → tmux keeps running
- Click `:8003` → browser opens to max's Django app
- Total time from "I want to know what jeffy was doing" to "I know" is < 3 seconds, no context switch into iTerm

---

## Phase A — Console foundation (full vision)

The smallest lovable console: open ranch, see your repos, see your agents, dispatch an interactive session, and watch it.

> **Status note (2026-04-29):** A1 is in review (#35). A4/A5/A6 are subsumed into Phase MVP (above) and will land there with a richer card spec. A2 (event bus) is deferred until polling proves insufficient. A3 (project registry) and A7 (interactive dispatch UI) are deferred until multi-project support is needed.

### A1. Electron app skeleton ✅ (PR #35)
- Electron + Vite + React + strict TypeScript
- Main process loads `~/.ranch/config.toml` and `~/.ranch/projects.toml`
- Sandboxed preload with `contextBridge` exposing typed `window.ranch.*` IPC surface
- Single-window four-pane shell (grid / terminal / inbox / memory)

### A2. RunEvent bus + schema
- Unified `RunEvent` envelope: `{run_id, ts, kind, payload, source}`
- Kinds: `session_started`, `session_idle`, `tool_use`, `checkpoint_pending`, `checkpoint_resolved`, `pr_comment_received`, `build_failed`, `build_passed`, `agent_blocked`, `agent_done`
- Bus implementation: unix socket (`~/.ranch/events.sock`) + DB persistence + fan-out to subscribers
- Hook-side publishers (Phase A4) and SDK-side publishers (Phase D1) both target this bus
- Renderer subscribes via Electron IPC (proxied from main)

### A3. Project registry
- `~/.ranch/projects.toml` with one entry per repo: path, label, project type (e.g. citemed_web style multi-service compose, single-service, etc.), pointer to `ranch.project.toml`
- "Add project" UX: pick a directory, ranch reads its `ranch.project.toml`, validates, registers
- Multi-repo support is first-class — ranch is not citemed_web-specific

### A4. Worktree grid (read-only first pass)
- One card per (project, agent) cell
- Shows: branch, ticket, dirty/clean, last commit, attached run state, attached docker stack state (if any), unread inbox count
- Pure read view — clicking a card opens detail; no dispatch yet
- Updates live from the event bus

### A5. Hook → event bus publishers
- `UserPromptSubmit`, `Stop`, `Notification`, `PostToolUse`, `SessionEnd` hooks all emit `RunEvent`s to the bus
- Replaces direct DB writes from hooks with bus writes (bus persists to DB internally)
- One canonical `publish_event(kind, payload)` helper imported by all hooks

### A6. PTY + xterm.js + tmux integration
- `node-pty` in main process spawns `tmux new-session -A -s ranch-<run_id> -- claude …` (idempotent attach)
- `xterm.js` + `@xterm/addon-webgl` in renderer
- Resize forwarding, clipboard, keybinding pass-through
- Closing the console window detaches tmux but does not kill it; reopening reattaches
- A6 is the load-bearing piece — most other features compose on top of it

### A7. Interactive dispatch UI
- "New session" modal: pick project, pick agent, ticket id (optional), brief (optional, becomes first user message)
- Pre-seeds the first message with brief + injected lessons + project context
- After the first message, the user drives — ranch is a launcher, not a wrapper

**Acceptance for Phase A:** Open ranch, see citemed_web with its 4 agents, dispatch an interactive session for max on ticket ECD-X, work in the embedded terminal exactly as you would in iTerm, close the window, reopen, see the session still alive, reattach.

---

## Phase B — Project config externalization

> **Deferred until post-MVP.** The hardcoded four-worktree setup that MVP reads from is sufficient day-to-day. Phase B becomes critical when (a) we want to onboard a fifth agent without editing citemed_web's Makefile, or (b) we want to use ranch on a second repo (citesource, scrapers). Until then, MVP-1 reads existing `.env.agent` files directly — no externalization needed.

Move per-project agent/port/worktree config out of application repos and into ranch.

### B1. `ranch.project.toml` schema + parser
- Declares: project name, worktree base, compose fragment paths, env template path, shared-infra fragment path, per-agent service names (so ranch knows which services need ports)
- Validation + clear error messages
- Lives at the repo root; checked into the application repo

### B2. Port ledger
- Dynamic allocation: ranch picks the next free port in a configurable range per service type
- Persisted in DB keyed by `(project, agent, service)`
- `ranch ports show` for visibility, `ranch ports release` for cleanup
- Conflict detection against running OS-level listeners
- (Decision needed: deterministic-by-hash vs dynamic-allocated. Default dynamic; deterministic available as fallback for environments that need stable URLs.)

### B3. Workspace lifecycle commands
- `ranch workspace create <agent> --project <name>` — creates worktree, allocates ports, writes `.env.agent` from template, runs migrations
- `ranch workspace destroy <agent> --project <name>` — tears down compose stack, removes worktree (with confirmation), releases ports
- `ranch workspace reset <agent> --project <name> --branch <branch>` — fresh task branch, clean DB schema
- `ranch workspace sync-env --project <name>` — re-syncs base env across all agent worktrees after secret rotation

### B4. citemed_web reference migration
- Strip the agent block from citemed_web's Makefile (lines 219–end of agent section)
- Add `ranch.project.toml` to citemed_web declaring the same shape
- Verify all four agents (jeffy/arnold/max/kesha) still bootstrap end-to-end via ranch
- Document the migration so other repos can follow

**Acceptance for Phase B:** Adding a fifth agent to citemed_web is a `ranch workspace create` away with no edits to citemed_web. Adding `citesource` as a second project is "drop a `ranch.project.toml` and register it" with no new ranch code.

---

## Phase C — Docker orchestration in console

> **Deferred until post-MVP.** Stack lifecycle stays in `make` for MVP — ranch displays state but doesn't control it. Phase C is what turns ranch into a true docker management surface; valuable but not on the day-one critical path. Becomes a priority once the operator finds themselves frequently context-switching to `make` commands while using ranch.

The docker pieces that today live in citemed_web's Makefile become first-class console features.

### C1. Per-project compose lifecycle
- "Start stack" / "Stop stack" / "Restart stack" buttons on each worktree card
- Wraps `docker compose --env-file <agent .env> -f <base> -f <agent compose> -p citemed_<agent>`
- Stream compose output to a per-stack log panel
- Surfaces compose failures (port conflicts, missing images) as inbox items

### C2. Shared infra lifecycle
- Console-managed shared infra (postgres, redis, network) — `ranch infra up/down/status`
- Auto-start when first agent stack starts; auto-stop never (operator opt-in)
- Health checks (postgres reachable, redis reachable) surfaced in the UI

### C3. Stack health + log tailing
- Per-service status indicators on each worktree card
- Click to expand: live log tail per service, with filtering
- "Open service shell" affordance for quick debugging

### C4. "Open in browser"
- For each agent's web-facing service, a button that opens `http://localhost:<port>` using the port ledger
- Multi-port support (Django + Vite usually, sometimes more)
- Shows port in the UI so the operator knows where to point manual curl/Postman calls

**Acceptance for Phase C:** Operator never types `make max && make max-vite` again. Bringing up agent max's stack is one click; opening max's app in the browser is one click; stopping it is one click.

---

## Phase D — Autonomous dispatch backend

> **Deferred until post-MVP.** The autonomous-mode killer use case (overnight build-fix loops) requires more infrastructure (event bus, supervision, CI watchers) than awareness MVP needs. Comes after Phase MVP proves the UX is right and after the SDK orchestrator has soaked through real use as the foundation.

Second adapter onto the same event bus, for fire-and-forget overnight work.

### D1. SDK orchestrator → RunEvent emitter
- Refactor `ranch/runner/orchestrator.py` to publish `RunEvent`s to the bus instead of (or in addition to) direct DB writes
- Existing checkpoint/decision/interjection plumbing becomes event consumers
- No functional change for users; clean separation for the console

### D2. Long-running run supervision
- Worker process supervisor: zombie detection, log rotation, restart policy
- Stop/resume controls in the console UI
- Resource budget per run (max wall-clock, max tokens, max push count)

### D3. Build-fix loop primitive
- New brief template: "fix the failing build, push, repeat until green or budget exhausted"
- CI watch: subscribe to Bitbucket/GitHub status checks → publish `build_failed` / `build_passed` events
- Agent waits on the next build status before iterating
- Budget controls: max N pushes, max M minutes, hard stop on flapping
- Use case: dispatch overnight, wake up to a green branch or a clear "stuck after 4 attempts" report

### D4. Run-mode selector in dispatch UI
- "New session" modal gets a mode toggle: Interactive / Autonomous
- Mode-specific options: budget for autonomous, brief required for both, auto-approve only available for autonomous
- Same dispatch endpoint, different backend; same card in the grid

**Acceptance for Phase D:** Dispatch an autonomous "fix builds on `feature/foo` until green" run, close ranch, come back in the morning, see either a green branch + diff in the inbox or a clear blocker report with retry history.

---

## Phase E — Inbox + run context

Make "where are we and what's been done?" answerable at a glance.

### E1. Unified inbox
- One stream of: PR comments, CI failures, idle agents, awaiting-approval checkpoints, autonomous-run completions, autonomous-run blockers
- Clicking an inbox item routes to the relevant context (run detail, PR, agent, etc.)
- Sourced from the event bus (Phase A2)

### E2. Run topic + status summary
- Each run gets a human-friendly topic (auto-derived from ticket title or brief, editable)
- Live one-line status: "drafting plan", "awaiting plan approval", "running tests", "tests green, awaiting pre-push approval", "blocked: rate limited", "done"
- Visible on the worktree grid card and in the run detail view

### E3. Run timeline
- Full event stream per run: prompts, tool uses, checkpoints, decisions, files touched, commits made, pushes, PRs opened
- Filterable by event kind
- Replaces "scroll the terminal" as the way to answer "what did this agent do?"

### E4. System notifications
- Native OS notifications for high-priority inbox items (awaiting approval, build broken, autonomous run done)
- Configurable per-event-kind quiet hours
- (Stretch: Slack/Discord routing for team awareness)

**Acceptance for Phase E:** Open ranch after lunch, glance at the inbox, see "max needs pre-push approval (2h ago), arnold's autonomous run finished green (45m ago), jeffy got 3 new PR comments (10m ago)" — no need to context-switch into terminals to know where everything stands.

---

## Phase F — Memory layer integration

The existing memory/lessons system becomes a console feature, not a separate CLI surface.

### F1. Memory panel
- Browse, search, edit, deactivate, merge lessons from the UI
- Bulk operations (deactivate all sub-confidence-2, merge near-duplicates)
- Confidence histograms, category breakdowns

### F2. Cross-project lesson scoping
- Lessons can be project-scoped (citemed_web only), agent-scoped (max only), or global
- Context injection respects scope
- Tagging UI to mark/correct scope as lessons accumulate

### F3. Lesson-in-context preview
- When dispatching, show which lessons will be injected before the session starts
- Edit/disable on a per-dispatch basis without touching the lesson DB

---

## Phase G — Production hardening (deferred)

When the console proves out, these become real concerns. Not before.

### G1. PostgreSQL migration
- Multiple agent processes writing concurrently hits SQLite's limits eventually
- SQLAlchemy already abstracts; need pooling config, alembic migrations, SQLite→PG migration tool
- Default stays SQLite for single-operator use

### G2. Cost tracking
- Token usage per run, per ticket, per project, per sprint
- Budget alerts and pause-on-threshold for autonomous runs
- Surfaced in run detail and rolled up in inbox

### G3. Rate limit awareness
- Cross-agent rate limit state — don't start new runs during global throttling
- Already have per-run retry; needs queue-level coordination

### G4. Specialization profiles (was issue #5)
- Per-agent success scores by domain
- Surface in dispatch UI as an "agent picker" hint
- Conflict detection: warn before dispatching two runs likely to touch overlapping files

---

## Explicit non-goals

- **Replacing the interactive Claude Code experience.** Plan mode, hooks, slash commands, the CC permission model — those stay where they are. Ranch is the console, not the IDE.
- **Multi-user/multi-tenant.** This is a single-operator local-first tool. If a team needs shared visibility, that's a different product.
- **Web/cloud dashboard.** A web app cannot embed pty, control docker, or watch the local file system. Electron is the correct chassis.
- **A general-purpose AI agent platform.** Ranch is opinionated about Claude Code as the agent runtime. Other agents would require a different abstraction layer that we're not building.

---

## Pickup order

```
✅ A1 (Electron skeleton, PR #35)
    │
    ├─ Phase MVP — ambient awareness  ← BUILD THIS NEXT
    │   MVP-1 (config bridge: read .env.agent for hardcoded 4 worktrees)
    │   MVP-2 (git state observer)
    │   MVP-3 (CC transcript → live TodoWrite state)
    │   MVP-4 (CC process + tmux session detection)
    │   MVP-5 (rich worktree grid card UI)
    │   MVP-6 (embedded terminal via tmux attach)
    │   MVP-7 (open-in-browser using port from .env.agent)
    │
    └─ post-MVP (sequenced after MVP ships and soaks)
        ├─ A2 (event bus)  ← when polling stops scaling
        ├─ A3 (project registry) + A7 (dispatch UI)  ← when 2nd project arrives
        ├─ B1..B4 (project config externalization)  ← when 5th agent / 2nd project arrives
        ├─ C1..C4 (docker orchestration)  ← when make-context-switching becomes annoying
        ├─ D1..D4 (autonomous mode)  ← after MVP ergonomics dial in
        ├─ E1..E4 (inbox + timeline)  ← needs A2 (event bus)
        └─ F1..F3 (memory panel)  ← opportunistic
```

**Phase MVP is the next thing built.** Everything else is real, queued, and intentional — but not on the day-one critical path. Each post-MVP phase has a concrete trigger (a real friction point) that should arise *during* MVP use; until that trigger fires, those phases stay deferred.

---

## Contributing

Ranch is an internal tool for the citemed team. If you're picking this up:

1. Read `USAGE.md` for the current CLI reference.
2. Run `pytest` to verify the test suite (58 tests).
3. The next thing built is **Phase MVP** — see the section above for the seven issues that compose it. Start with MVP-1 (config bridge) and MVP-3 (transcript parser) in parallel; they unblock MVP-5 (the card UI).
