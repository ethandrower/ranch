"""Dossier persistence via PostToolUse hook.

Captures `record_state` payloads emitted by the agent (Phase H1 tool) and
writes them as `Dossier` rows. Unlike the checkpoint hook, this hook is
non-blocking — dossier updates are informational, never approval gates.
"""
from __future__ import annotations

from claude_code_sdk import HookMatcher
from claude_code_sdk.types import HookContext
from pydantic import ValidationError

from ranch.runner.messages import RecordStateInput

STATE_TOOL = "mcp__ranch__record_state"


def make_dossier_hook(orchestrator) -> HookMatcher:
    """Return a HookMatcher that fires on record_state tool calls."""

    async def on_post_tool_use(
        input_data: dict,
        tool_use_id: str | None,
        context: HookContext,
    ) -> dict:
        tool_name = input_data.get("tool_name", "")
        if tool_name != STATE_TOOL:
            return {}

        try:
            dossier = RecordStateInput.model_validate(input_data.get("tool_input") or {})
        except ValidationError as exc:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": f"record_state validation error: {exc}",
                }
            }

        await orchestrator.on_state(dossier)
        return {}

    return HookMatcher(matcher=STATE_TOOL, hooks=[on_post_tool_use])
