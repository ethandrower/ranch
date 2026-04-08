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


@tool("record_checkpoint", "Record a checkpoint. Use when you've finished planning, tests pass, or before pushing.", CHECKPOINT_INPUT_SCHEMA)
async def record_checkpoint(args: dict) -> dict:
    # The orchestrator intercepts this via PostToolUse hook.
    # We just echo back so the model's tool result is clean.
    return {"content": [{"type": "text", "text": f"Checkpoint '{args['kind']}' recorded."}]}


@tool("log_decision", "Log a non-trivial implementation decision for human review.", DECISION_INPUT_SCHEMA)
async def log_decision(args: dict) -> dict:
    return {"content": [{"type": "text", "text": "Decision logged."}]}


ranch_mcp = create_sdk_mcp_server(
    name="ranch",
    version="0.1.0",
    tools=[record_checkpoint, log_decision],
)
