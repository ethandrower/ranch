"""In-process MCP tools exposed to the agent during a run."""
from pathlib import Path

from claude_code_sdk import tool, create_sdk_mcp_server

CHECKPOINT_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["plan_ready", "tests_green", "pre_push", "custom"],
            "description": "The type of checkpoint.",
        },
        "summary": {
            "type": "string",
            "description": "A 1-3 sentence human-readable summary of what was accomplished.",
        },
        "payload": {
            "type": "object",
            "description": "Optional structured data (diff stats, file list, etc).",
        },
    },
    "required": ["kind", "summary"],
}

DECISION_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "description": "The decision or choice made.",
        },
        "rationale": {
            "type": "string",
            "description": "Why this decision was made.",
        },
    },
    "required": ["decision", "rationale"],
}

STATE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "plan": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "step": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "done"],
                    },
                    "notes": {"type": "string"},
                },
                "required": ["step", "status"],
            },
            "description": "Ordered plan steps with current status.",
        },
        "just_did": {
            "type": "string",
            "description": "One or two sentences summarizing your most recent action, in plain English (not raw tool calls).",
        },
        "state": {
            "type": "string",
            "enum": ["researching", "planning", "coding", "testing", "judging", "parked"],
            "description": "What phase are you in right now.",
        },
        "blocker": {
            "type": "string",
            "description": "If state=parked, what's blocking you. Otherwise omit.",
        },
        "options": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["label", "description"],
            },
            "description": "If parked and human needs to decide, the choices to surface.",
        },
        "files_touched": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Files modified so far this run.",
        },
        "ticket": {
            "type": "string",
            "description": "Ticket identifier (e.g. ECD-1234), if known.",
        },
        "details": {
            "type": "string",
            "description": (
                "Optional long-form narrative for this step — what you attempted, "
                "results, decisions, issues, conclusions. This is what the operator "
                "reads when they expand this stage in the UI. Use when the step is "
                "non-trivial; omit for routine transitions."
            ),
        },
        "acceptance": {
            "type": "array",
            "description": (
                "Machine-verifiable acceptance criteria for the ticket. Set during "
                "`ranch propose` (H6); consumed by the `run_acceptance` tool during "
                "execute (H8). Each check is independently runnable. Browser + "
                "figma diff are NOT yet supported — use unit_test / script / http."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["unit_test", "script", "http"],
                    },
                    "name": {"type": "string"},
                    "cmd": {
                        "type": "string",
                        "description": "Shell command for unit_test / script kinds.",
                    },
                    "pass_pattern": {
                        "type": "string",
                        "description": "Substring or regex in stdout that proves pass. Required for unit_test / script.",
                    },
                    "url": {
                        "type": "string",
                        "description": "HTTP endpoint to probe (http kind).",
                    },
                    "expected_status": {"type": "integer"},
                    "expected_body_contains": {"type": "string"},
                    "timeout_seconds": {"type": "number", "default": 60},
                },
                "required": ["kind", "name"],
            },
        },
    },
    "required": ["plan", "just_did", "state"],
}


@tool("record_checkpoint", "Record a checkpoint. Use when you've finished planning, tests pass, or before pushing.", CHECKPOINT_INPUT_SCHEMA)
async def record_checkpoint(args: dict) -> dict:
    # The orchestrator intercepts this via PostToolUse hook.
    # We just echo back so the model's tool result is clean.
    return {"content": [{"type": "text", "text": f"Checkpoint '{args['kind']}' recorded."}]}


@tool("log_decision", "Log a non-trivial implementation decision for human review.", DECISION_INPUT_SCHEMA)
async def log_decision(args: dict) -> dict:
    return {"content": [{"type": "text", "text": "Decision logged."}]}


@tool(
    "record_state",
    "Update your dossier — a structured self-report of where you are right now (plan, what you just did, phase, any blocker). Call this when finalizing a plan, completing a plan step, switching phases, or before parking.",
    STATE_INPUT_SCHEMA,
)
async def record_state(args: dict) -> dict:
    # H2 will wire the orchestrator to capture this payload and persist it.
    # For now the tool just acknowledges so the agent's tool result is clean.
    return {"content": [{"type": "text", "text": "Dossier updated."}]}


# ─── H8: run_acceptance ────────────────────────────────────────────


# Per-process budget guard — refuses past this many calls within a single
# orchestrator session. Prevents the "agent stuck in an iterate-until-pass
# loop" failure mode. Reset by `reset_judge_budget()` at session start.
DEFAULT_JUDGE_BUDGET = 8
_judge_call_count = 0


def reset_judge_budget() -> None:
    """Called by the orchestrator at the start of each run."""
    global _judge_call_count
    _judge_call_count = 0


def _judge_budget_remaining() -> int:
    return DEFAULT_JUDGE_BUDGET - _judge_call_count


RUN_ACCEPTANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "checks": {
            "type": "array",
            "description": (
                "Inline acceptance checks to run. If omitted, the tool reads "
                "the latest record_state's `acceptance` field (set during "
                "ranch propose). Provide inline only when you want to verify "
                "an ad-hoc check during development."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["unit_test", "script", "http"]},
                    "name": {"type": "string"},
                    "cmd": {"type": "string"},
                    "pass_pattern": {"type": "string"},
                    "url": {"type": "string"},
                    "expected_status": {"type": "integer"},
                    "expected_body_contains": {"type": "string"},
                    "timeout_seconds": {"type": "number"},
                },
                "required": ["kind", "name"],
            },
        },
        "cwd": {
            "type": "string",
            "description": "Override the working directory. Defaults to the run's worktree.",
        },
    },
}


@tool(
    "run_acceptance",
    (
        "Run the ticket's acceptance checks (unit tests, scripts, HTTP probes) "
        "and report pass/fail per check. Use this after you've made changes to "
        "verify your work objectively before parking at pre_push. If a check "
        "fails, the result includes its stdout/stderr — read it, fix the issue, "
        "and call run_acceptance again. Budget: limited calls per session, so "
        "don't burn calls on speculative runs."
    ),
    RUN_ACCEPTANCE_SCHEMA,
)
async def run_acceptance(args: dict) -> dict:
    """Run the acceptance checks. The orchestrator's PostToolUse hook
    actually executes them (it has access to the run's cwd + dossier);
    the body here just records the budget tick and returns a placeholder
    that the hook replaces via additionalContext."""
    global _judge_call_count
    _judge_call_count += 1
    remaining = _judge_budget_remaining()
    if remaining < 0:
        return {
            "content": [{
                "type": "text",
                "text": (
                    "JUDGE BUDGET EXHAUSTED. You've called run_acceptance too "
                    "many times this session. Park at state=parked with "
                    "blocker='stuck_judge_budget_exhausted' and let the operator "
                    "review."
                ),
            }],
        }
    # The hook injects the real results as additionalContext; this echo is
    # a sentinel the agent will see if the hook didn't fire (e.g., misconfig).
    return {
        "content": [{
            "type": "text",
            "text": f"(run_acceptance pending hook execution — call {_judge_call_count}/{DEFAULT_JUDGE_BUDGET})",
        }],
    }


ranch_mcp = create_sdk_mcp_server(
    name="ranch",
    version="0.1.0",
    tools=[record_checkpoint, log_decision, record_state, run_acceptance],
)
