"""In-process MCP tools exposed to the agent during a run."""
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


ranch_mcp = create_sdk_mcp_server(
    name="ranch",
    version="0.1.0",
    tools=[record_checkpoint, log_decision, record_state],
)
