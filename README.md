# Ranch

A memory and learning layer for [Claude Code](https://claude.ai/code) agent fleets.

**→ [USAGE.md](USAGE.md) — commands, flags, and examples at a glance**

---

Ranch sits alongside your Claude Code worktrees and does two things:

1. **Learns** — captures every correction you make during a session, distills them into reusable lessons via reflection, and injects relevant lessons into new sessions so your agents start each ticket already knowing what you care about.

2. **Orchestrates** — runs agents via `ranch run` with structured checkpoints, streaming output, and mid-run interjections (`!approve`, `!reject`, `!note`, `!stop`).

```
corrections → episodic memory → reflection → semantic lessons → context injection
brief → ranch run → plan checkpoint → develop → pre-push checkpoint → done
```

---

## Install

Requires Python 3.11+ and a Claude Code installation with an `ANTHROPIC_API_KEY`.

```bash
git clone https://github.com/yourname/ranch
cd ranch
uv venv --python python3.11 .venv
uv pip install -e .
```

### Configure your agents

On first `ranch init`, a starter `~/.ranch/config.toml` is created:

```toml
[agents.my-agent]
worktree = "/path/to/my/worktree"
description = "Optional label"
```

Add one section per Claude Code worktree you want to track.

### Install hooks

Add to `~/.claude/settings.json` (use absolute paths to your venv Python):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/ranch/.venv/bin/python /path/to/ranch/hooks/capture_feedback.py"
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/ranch/.venv/bin/python /path/to/ranch/hooks/reflect_on_end.py"
          }
        ]
      }
    ]
  }
}
```

### Verify

```bash
ranch init     # creates DB + config, checks worktrees
ranch status   # fleet overview
```

---

## Usage

See **[USAGE.md](USAGE.md)** for the full command reference.

Quick start:

```bash
# Supervised ticket run with checkpoints
ranch run max --ticket ECD-123 --brief "Add /healthz endpoint"

# Open-ended task (PR review, bug investigation, etc.)
ranch run max --ticket ECD-456 --free \
  --brief "PR #89 is open. Check review comments and reply, making code changes where correct."

# Inject lessons into a manual CC session
ranch context --tags django,api > /tmp/ctx.md
# paste contents into your first CC message

# View what's been learned
ranch lessons
```

---

## How it works

### Memory (Phase 1)

The `UserPromptSubmit` and `SessionEnd` hooks fire on every Claude Code interaction. Messages on ticket branches (any branch containing a pattern like `ECD-123`, `AI-99`, `PROJ-456`) are saved as **feedback** rows. When a session ends or you switch branches, a reflection agent reads the feedback and extracts **lessons** — reusable, actionable patterns stored by category and confidence score.

Branch formats supported:

```
feature/ECD-1476-some-thing   → ECD-1476
ECD-1476                      → ECD-1476
AI-123-my-feature             → AI-123
hotfix/PROJ-200               → PROJ-200
```

### Orchestrator (Phase 2)

`ranch run` starts a bidirectional Claude Code SDK session. The agent is given a system prompt that enforces a structured workflow and a set of MCP tools it calls to signal progress:

| Tool | When the agent calls it |
|---|---|
| `record_checkpoint(kind="plan_ready")` | Finished planning, waiting for approval |
| `record_checkpoint(kind="tests_green")` | Tests pass, continuing to QA |
| `record_checkpoint(kind="pre_push")` | Ready to push, waiting for approval |
| `log_decision(...)` | Recording a non-trivial implementation choice |

`plan_ready` and `pre_push` pause the run and wait for `!approve` or `!reject`. Everything is written to the DB so you can `ranch runs` to see history and `ranch resume <id>` to continue a paused session.

Use `--free` to skip the enforced workflow entirely — the brief becomes the complete instruction with no assumed steps.

---

## Architecture

```
ranch/
├── config.py           # paths, loads agent registry from ~/.ranch/config.toml
├── db.py               # SQLAlchemy engine + session context manager
├── models.py           # Ticket, Feedback, Lesson, ReflectionRun, Run, Checkpoint, Interjection
├── transcript.py       # parse CC session JSONL transcripts
├── feedback.py         # episodic memory: log + query
├── lessons.py          # semantic memory: create, reinforce, query
├── reflect.py          # reflection runner (calls Claude via claude-code-sdk)
├── reflect_cli.py      # subprocess entry point used by hooks
├── context.py          # build lesson-injection markdown for new sessions
├── cli.py              # Click CLI
└── runner/
    ├── orchestrator.py # ClaudeSDKClient wrapper, streaming loop, pause/resume
    ├── checkpoints.py  # PostToolUse HookMatcher
    ├── tools.py        # MCP tools (record_checkpoint, log_decision)
    ├── state.py        # run state machine
    └── prompts.py      # system prompts (standard + free)

hooks/
├── capture_feedback.py   # UserPromptSubmit hook
└── reflect_on_end.py     # SessionEnd hook
```

**Database:** SQLite by default at `~/.ranch/ranch.db`. Swap to PostgreSQL via `RANCH_DATABASE_URL` env var.

---

## Tuning

The reflection prompt in `ranch/reflect.py` (`REFLECTION_PROMPT`) is the highest-leverage thing to tune. The default is conservative — 0–3 lessons per ticket, skips one-offs. Edit it if you're getting noise or missing real patterns.

After a week of use, audit your lessons:

```bash
ranch lessons
```

Manually adjust confidence or deactivate noise:

```bash
sqlite3 ~/.ranch/ranch.db "UPDATE lessons SET is_active=0 WHERE id=42"
sqlite3 ~/.ranch/ranch.db "UPDATE lessons SET confidence=4 WHERE id=7"
```

---

## License

MIT
