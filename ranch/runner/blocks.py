"""Block persistence via PostToolUse hook.

Captures `record_block` payloads emitted by the agent and writes them as
Block rows. Mirrors runner/dossier.py but for blocks.
"""
from __future__ import annotations

from claude_code_sdk import HookMatcher
from claude_code_sdk.types import HookContext
from pydantic import ValidationError

from ranch.blocks import record_block as _record_block
from ranch.runner.messages import RecordBlockInput

BLOCK_TOOL = "mcp__ranch__record_block"


def make_block_hook(orchestrator) -> HookMatcher:
    """Return a HookMatcher that fires on record_block tool calls."""

    async def on_post_tool_use(
        input_data: dict,
        tool_use_id: str | None,
        context: HookContext,
    ) -> dict:
        tool_name = input_data.get("tool_name", "")
        if tool_name != BLOCK_TOOL:
            return {}

        try:
            payload = RecordBlockInput.model_validate(input_data.get("tool_input") or {})
        except ValidationError as exc:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": f"record_block validation error: {exc}",
                }
            }

        if orchestrator.run_id is None:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": "record_block ignored — orchestrator has no run_id yet.",
                }
            }

        _record_block(
            blocked_run_id=orchestrator.run_id,
            blocker_ticket=payload.blocker_ticket,
            reason=payload.reason,
            source="agent",
        )
        return {}

    return HookMatcher(matcher=BLOCK_TOOL, hooks=[on_post_tool_use])
