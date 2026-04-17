"""Checkpoint pause logic via PostToolUse hook.

The hook itself awaits the human decision (auto- or interactive-) and returns
the typed decision message as additionalContext on the SAME tool-result. This
guarantees the agent sees the decision attached to its own tool call — there's
no race with concurrent SDK system notifications (background-task completions,
etc.) that could otherwise drown out a follow-up user message.
"""
from __future__ import annotations
from claude_code_sdk import HookMatcher
from claude_code_sdk.types import HookContext
from pydantic import ValidationError

from ranch.runner.messages import CheckpointInput, HumanDecision

CHECKPOINT_TOOL = "mcp__ranch__record_checkpoint"
APPROVAL_REQUIRED = {"plan_ready", "pre_push", "triage"}


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

        try:
            cp = CheckpointInput.model_validate(input_data.get("tool_input") or {})
        except ValidationError as exc:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": f"record_checkpoint validation error: {exc}",
                }
            }

        await orchestrator.on_checkpoint(cp.kind, cp.summary, cp.payload)

        if cp.kind not in APPROVAL_REQUIRED:
            return {}

        # Block here until a decision arrives (auto-approve fires immediately
        # from on_checkpoint; interactive mode waits for !approve / !reject).
        await orchestrator._approval_ready.wait()
        orchestrator._approval_ready.clear()
        orchestrator._awaiting_approval = False

        raw = orchestrator._approval_result or "approved"
        orchestrator._approval_result = None
        is_rejected = raw.startswith("rejected")
        reason = raw.removeprefix("rejected — ").strip() if is_rejected else None

        # Persist the decision to DB now that we know it ties to this checkpoint.
        orchestrator._record_decision(
            "rejected" if is_rejected else "approved", reason or ""
        )

        decision_msg = HumanDecision(
            checkpoint_kind=cp.kind,
            decision="rejected" if is_rejected else "approved",
            reason=reason,
            ticket=orchestrator.ticket,
        ).to_prompt()

        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": decision_msg,
            }
        }

    return HookMatcher(matcher=CHECKPOINT_TOOL, hooks=[on_post_tool_use])
