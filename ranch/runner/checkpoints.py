"""Checkpoint pause logic via PostToolUse hook."""
from __future__ import annotations
from claude_code_sdk import HookMatcher
from claude_code_sdk.types import HookContext

CHECKPOINT_TOOL = "mcp__ranch__record_checkpoint"
APPROVAL_REQUIRED = {"plan_ready", "pre_push"}


def make_checkpoint_hook(orchestrator) -> HookMatcher:
    """Return a HookMatcher that fires on record_checkpoint tool calls."""

    async def on_post_tool_use(
        input_data: dict,
        tool_use_id: str | None,
        context: HookContext,
    ) -> dict:
        tool_name = input_data.get("tool_name", "")
        if tool_name != CHECKPOINT_TOOL:
            return {}

        args = input_data.get("tool_input", {}) or {}
        kind = args.get("kind", "custom")
        summary = args.get("summary", "")
        payload = args.get("payload")

        await orchestrator.on_checkpoint(kind, summary, payload)

        if kind in APPROVAL_REQUIRED:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": (
                        f"PAUSED at checkpoint '{kind}'. "
                        "Do NOT continue until you receive an explicit human decision. "
                        "Your next user message will contain 'Human decision: approved' or "
                        "'Human decision: rejected — <reason>'."
                    ),
                }
            }
        return {}

    return HookMatcher(matcher=CHECKPOINT_TOOL, hooks=[on_post_tool_use])
