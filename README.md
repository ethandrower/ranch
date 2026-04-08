# Ranch

A memory and learning layer for [Claude Code](https://claude.ai/code) agent fleets.

Ranch sits alongside your Claude Code worktrees and automatically captures every correction you make during a session. When a session ends or you switch branches, it runs a reflection pass that distills raw corrections into reusable lessons. Those lessons can be injected into new sessions so your agents start each ticket already knowing what you care about.

```
corrections → episodic memory → reflection → semantic lessons → context injection
```

---

## How it works

1. **Hooks** (`UserPromptSubmit`, `SessionEnd`) fire on every Claude Code interaction
2. Messages on ticket branches (e.g. `ECD-123`, `AI-99`, `feature/PROJ-456-thing`) are saved as **feedback** rows
3. When a session ends or you switch branches, a reflection agent (Claude via the SDK) reads the feedback and extracts **lessons** — reusable, actionable patterns
4. Before starting a new ticket, `ranch context` prints a markdown block of relevant lessons you can paste into your first message

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

Ranch stores all data in `~/.ranch/`. On first `ranch init`, it creates a starter `~/.ranch/config.toml`:

```toml
[agents.my-agent]
worktree = "/path/to/my/worktree"
description = "Optional label"

[agents.another-agent]
worktree = "/path/to/another/worktree"
```

Add one section per Claude Code worktree you want to track.

### Install hooks

Add the following to `~/.claude/settings.json`, using the absolute path to your venv Python:

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

### Day-to-day

```bash
# Work on a ticket branch as normal — feedback is captured automatically
git checkout -b ECD-123-my-feature
# ... open Claude Code, work, make corrections ...

# Reflection fires automatically on session end or branch switch.
# Force it manually:
ranch reflect          # uses current branch
ranch reflect ECD-123  # specific ticket

# Before starting a new ticket, grab relevant lessons:
ranch context --tags django,api > /tmp/lessons.md
# paste the contents into your first CC message
```

### Inspect the memory

```bash
ranch feedback               # recent raw corrections
ranch feedback --limit 50

ranch lessons                # semantic lessons by confidence
ranch lessons --category django_specific
```

---

## Branch name formats

Ranch detects ticket IDs anywhere in the branch name using the pattern `[A-Z][A-Z0-9]+-\d+`. All of these work:

```
feature/ECD-1476-some-thing   → ECD-1476
ECD-1476-some-thing           → ECD-1476
ECD-1476                      → ECD-1476
AI-123-my-feature             → AI-123
hotfix/PROJ-200               → PROJ-200
```

Branches with no ticket ID (e.g. `main`, `dev`) are silently ignored.

---

## Data

All data lives in `~/.ranch/` — outside the repo, shared across all worktrees:

| File | Contents |
|---|---|
| `ranch.db` | SQLite database (tickets, feedback, lessons, reflection runs) |
| `config.toml` | Agent registry |
| `active_tickets.json` | Session → ticket state for the hooks |
| `reflection.log` | Output from async reflection runs |
| `hook_errors.log` | Hook errors (hooks never crash CC) |

---

## Architecture

```
ranch/
├── config.py       # paths, loads agent registry from ~/.ranch/config.toml
├── db.py           # SQLAlchemy engine + session context manager
├── models.py       # Ticket, Feedback, Lesson, ReflectionRun
├── transcript.py   # parse CC session JSONL transcripts
├── feedback.py     # episodic memory: log + query
├── lessons.py      # semantic memory: create, reinforce, query
├── reflect.py      # reflection runner (calls Claude via claude-code-sdk)
├── reflect_cli.py  # subprocess entry point used by hooks
├── context.py      # build lesson-injection markdown for new sessions
└── cli.py          # Click CLI

hooks/
├── capture_feedback.py   # UserPromptSubmit hook
└── reflect_on_end.py     # SessionEnd hook
```

**Database:** SQLite by default. Swap to PostgreSQL by setting `RANCH_DATABASE_URL`.

---

## Tuning

The reflection prompt in `ranch/reflect.py` (`REFLECTION_PROMPT`) is the highest-leverage thing to tune. The default prompt is conservative — it asks for 0–3 lessons per ticket and skips one-offs. If you're getting too much noise or missing real patterns, edit it there.

After a week of use:

```bash
ranch lessons   # read every lesson out loud — are they useful?
```

Manually bump `confidence` (1–5) on lessons you trust. Delete noise directly in SQLite:

```bash
sqlite3 ~/.ranch/ranch.db "UPDATE lessons SET is_active=0 WHERE id=42"
```

---

## License

MIT
