# Ranch Roadmap

Ranch is an orchestration and learning layer for Claude Code agent fleets. It manages multiple agent worktrees for the citemed project, providing memory capture, lesson injection, and a checkpointed orchestrator.

---

## Current Status

**Phases 0 through 2+ are complete. 58 tests passing.**

Ranch can run a single agent session end-to-end with a structured plan-TDD-QA-pre-push workflow, capture corrections, distill them into reusable lessons, and inject those lessons into future sessions. The orchestrator supports streaming output, mid-run interjections, checkpoint approval/rejection, session resume, and free-form mode.

Phase 2+ hardened the orchestrator with rate-limit retry, Pydantic message contracts, hook-based approval delivery, `--auto-approve` for unattended runs, daemon-thread stdin, `append_system_prompt` for worktree CLAUDE.md loading, test DB isolation, and `bb` CLI integration.

**What's missing:** runs block the terminal. You cannot dispatch work to multiple agents simultaneously and monitor them from a single session. There is no cross-agent learning, no PR feedback loop, no dashboard, and no production-grade infrastructure.

---

## What We Learned

Key architectural decisions made during Phases 1-2+:

- **Hook-based approval delivery.** Checkpoint approval is delivered via a PostToolUse hook that awaits the human decision and returns it as `additionalContext`. This eliminates the race condition between SDK system notifications and the approval payload, giving deterministic delivery.

- **`append_system_prompt` for CLAUDE.md.** Each worktree has its own CLAUDE.md with branch-off-develop enforcement and project conventions. Rather than injecting this into the brief, we append it via the SDK's `append_system_prompt` parameter so it behaves like a native system prompt and doesn't pollute the user message.

- **Pydantic message contracts.** `CheckpointInput`, `HumanDecision`, and `HumanNote` are Pydantic models that validate all data flowing between the orchestrator, hooks, and stdin loop. This caught serialization bugs early and makes the protocol self-documenting.

- **Branch-off-develop enforcement.** The worktree CLAUDE.md instructs the agent to always branch off `develop`, never `main`. This prevents accidental pushes to the release branch and keeps the git workflow consistent across all agents.

- **Test DB isolation.** A `conftest.py` fixture creates a fresh in-memory SQLite database per test, ensuring tests never leak state. This was necessary after early test failures caused by shared DB state.

---

## Phase 0: Foundation (done)

- SQLAlchemy/SQLite database with models for Ticket, Feedback, Lesson, ReflectionRun
- Agent registry via `~/.ranch/config.toml`
- CLI scaffold with Click (`ranch init`, `ranch status`)

## Phase 1: Memory Capture (done)

- `UserPromptSubmit` hook captures corrections as episodic feedback
- `SessionEnd` hook triggers reflection, which distills feedback into reusable lessons
- `ranch context` builds lesson-injection markdown for new sessions
- Lessons stored with category, tags, confidence score
- Ticket ID extraction from branch names (ECD-123, AI-99, PROJ-456 patterns)

## Phase 2: Checkpointed Orchestrator (done)

- `ranch run` starts a Claude Code SDK session with structured workflow
- Checkpoints: `plan_ready`, `tests_green`, `pre_push`, `custom`
- Streaming output with real-time display
- Mid-run interjections: `!approve`, `!reject`, `!note`, `!stop`
- `ranch resume <id>` to continue paused sessions
- `ranch runs` to list run history
- `--free` flag for open-ended tasks (no enforced workflow)

## Phase 2+: Hardening (done)

- Rate-limit event retry logic
- Pydantic message contracts (CheckpointInput, HumanDecision, HumanNote)
- Hook-based approval delivery via PostToolUse
- `--auto-approve` flag for unattended evaluation runs
- Daemon-thread stdin loop with clean process exit
- `append_system_prompt` for worktree CLAUDE.md injection
- Test DB isolation via conftest.py
- `bb` CLI integration (Bitbucket CLI, gh-style)
- 58 tests passing

---

## Phase 3: Fleet Dispatch & Monitoring

The biggest gap today: `ranch run` blocks the terminal. You can only run one agent at a time. Phase 3 makes Ranch a true fleet manager.

### 3a. Background Dispatch

`ranch dispatch <agent> --ticket <id> --brief <text>` starts a run in the background and returns immediately. Output is logged to a file. The run record in the DB tracks the PID and log path.

### 3b. Watch / Wait

`ranch watch` blocks until any running agent finishes, then reports which agent completed and the outcome (success, failure, awaiting approval). This enables a "foreman" pattern: dispatch N agents, then `watch` in a loop to handle completions as they arrive.

### 3c. Batch Dispatch

`ranch dispatch-batch --file tickets.csv` or similar takes a list of tickets and auto-assigns them to idle agents. Requires idle detection (3d).

### 3d. Idle Agent Detection

`ranch status` already exists but reports static config. Enhance it with real-time awareness: is the agent currently running a task? What's the PID? When did it start? This is the foundation for batch dispatch and the dashboard.

### 3e. Jira/Issue Tracker Integration (stretch)

Auto-pick tickets from the sprint board. Lower priority than the core dispatch loop.

---

## Phase 4: Cross-Agent Learning & Specialization

Currently each agent learns independently. If jeffy learned about the citesource export system, max has no access to that knowledge.

### 4a. Shared Lesson Pool

Lessons are already in a shared SQLite DB, but the context injection only considers the current agent's feedback history. Extend `ranch context` and the automatic injection to pull from the global lesson pool, optionally filtered by relevance to the current ticket's domain.

### 4b. Agent Specialization Profiles

Track which agent succeeds most often in which area (frontend, backend, scrapers, data pipeline). Use this to inform auto-assignment in batch dispatch. Could be as simple as tagging lessons/runs with domain categories and computing a per-agent score.

### 4c. Conflict Detection

Before dispatching two agents, check if their tickets are likely to touch the same files or modules. Warn the operator or serialize the work. Could use file-change history from past runs or static analysis of the ticket brief.

---

## Phase 5: PR Feedback Loop

After an agent pushes a branch and creates a PR, the work isn't done. Reviewers leave comments. Today those comments are manually relayed. Phase 5 closes the loop.

### 5a. Poll for Review Comments

After a PR is created, periodically poll `bb pr view <id> --comments` (or `gh pr view`) for new review feedback. Store comments in the DB linked to the run.

### 5b. Auto-Feed Comments to Agent

When new comments arrive, start (or resume) an agent session with the review feedback as the brief. The agent reads the comments, makes fixes, and pushes again.

### 5c. Webhook-Based Notification (stretch)

Replace polling with Bitbucket/GitHub webhooks for real-time PR comment notifications. Requires a small HTTP server or integration with an existing webhook receiver.

---

## Phase 6: Web Dashboard

A visual interface for fleet management.

### 6a. FastAPI Backend

REST API exposing runs, agents, lessons, feedback. WebSocket endpoint for real-time streaming of agent output.

### 6b. Frontend

Simple web UI (could be React, htmx, or even a terminal UI). Visualize: active runs, agent status grid, lesson browser, feedback timeline.

### 6c. Manual Dispatch from UI

Start runs, approve/reject checkpoints, and send interjections from the browser instead of the CLI.

### 6d. Lesson Management

View, edit, deactivate, and merge lessons from the UI. Bulk operations for cleanup after a sprint.

---

## Phase 7: Production Hardening

Move from single-user SQLite to a production-grade deployment.

### 7a. PostgreSQL Migration

SQLAlchemy already abstracts the DB, but SQLite has concurrency limits that matter once multiple agents write simultaneously. Migrate to PostgreSQL and add connection pooling.

### 7b. Multi-User Support & Auth

API keys or OAuth for the dashboard. Per-user agent pools and lesson namespaces.

### 7c. Rate Limit Awareness & Queuing

The orchestrator already retries on rate limits. Extend this to queue-level awareness: if the API is throttled, don't start new agent runs. Share rate-limit state across agents.

### 7d. Cost Tracking & Budgets

Track token usage per run, per ticket, per sprint. Set budgets and alert or pause when thresholds are exceeded.

### 7e. Metrics & Observability

How long do runs take? What's the success rate? Which checkpoints get rejected most often? Export metrics to Prometheus/Grafana or build simple charts in the dashboard.

---

## Contributing

Ranch is an internal tool for the citemed team. If you're picking this up:

1. Read `USAGE.md` for the CLI reference.
2. Run `pytest` to verify the test suite (58 tests).
3. The highest-leverage next step is Phase 3a (background dispatch) — it unblocks everything else.
