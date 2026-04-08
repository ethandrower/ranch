"""Typed message contracts for ranch ↔ agent communication.

Outbound  (ranch → agent):  HumanDecision, HumanNote
Inbound   (agent → ranch):  CheckpointInput, DecisionLogInput
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, field_validator


# ─── Inbound: agent calls these MCP tools ────────────────────────────────────


class CheckpointInput(BaseModel):
    """Payload the agent sends when calling mcp__ranch__record_checkpoint."""

    kind: Literal["plan_ready", "tests_green", "pre_push", "custom"]
    summary: str
    payload: Optional[dict[str, Any]] = None

    @field_validator("summary")
    @classmethod
    def summary_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("summary must not be empty")
        return v


class DecisionLogInput(BaseModel):
    """Payload the agent sends when calling mcp__ranch__log_decision."""

    decision: str
    rationale: str


# ─── Outbound: ranch → agent ──────────────────────────────────────────────────


class HumanDecision(BaseModel):
    """Structured approval or rejection sent to the agent after a checkpoint pause.

    Use .to_prompt() to render as agent-facing text.
    """

    checkpoint_kind: Literal["plan_ready", "tests_green", "pre_push", "custom"]
    decision: Literal["approved", "rejected"]
    reason: Optional[str] = None  # populated on rejection
    ticket: Optional[str] = None  # used to generate branch name hint on pre_push

    def to_prompt(self) -> str:
        """Render as unambiguous agent-facing text."""
        lines = [
            f"HUMAN DECISION on `{self.checkpoint_kind}`: {self.decision.upper()}",
        ]

        if self.decision == "rejected":
            lines.append(f"Reason: {self.reason or '(no reason given)'}")
            lines.append("Please revise and re-record the checkpoint when ready.")
            return "\n".join(lines)

        # Approved — add checkpoint-specific next-step instructions
        if self.checkpoint_kind == "plan_ready":
            lines.append("Plan approved. Proceed to DEVELOP: write failing tests first, then the implementation.")

        elif self.checkpoint_kind == "tests_green":
            lines.append("Tests green. Proceed to QA: re-read the diff, run linters.")

        elif self.checkpoint_kind == "pre_push":
            branch_hint = f"{self.ticket.lower()}-fix" if self.ticket else "<ticket-id>-fix"
            lines += [
                "Pre-push approved. Complete the push now:",
                f"1. Create branch `{branch_hint}` if not already on the right ticket branch.",
                "2. Stage all your ticket's files. Run `git status` AFTER any auto-formatting "
                "(ruff/black/etc. un-stage files they modify — re-add them).",
                "3. Exclude unrelated files (migrations from other apps, lock files, etc.).",
                f"4. Commit: `{self.ticket}: <one-line summary>`" if self.ticket else "4. Commit with a clear message.",
                "5. Push to origin and open a PR.",
            ]

        elif self.checkpoint_kind == "custom":
            lines.append("Approved. Continue.")

        return "\n".join(lines)


class HumanNote(BaseModel):
    """A mid-run human note forwarded to the agent, not tied to any checkpoint."""

    content: str

    def to_prompt(self) -> str:
        return f"[Human note mid-run]: {self.content}"
