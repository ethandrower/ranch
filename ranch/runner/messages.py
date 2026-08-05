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


class RecordBlockInput(BaseModel):
    """Payload the agent sends when calling mcp__ranch__record_block.

    Use during propose/triage when the current ticket's plan depends on a
    decision pending in another ticket. The hand scheduler skips runs that
    have unresolved blocks pointing at them; the block auto-resolves when
    the blocker ticket gets a checkpoint approval. Operator may override
    via `ranch unblock <run_id>`.
    """

    blocker_ticket: str
    reason: str

    @field_validator("blocker_ticket")
    @classmethod
    def blocker_ticket_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("blocker_ticket must not be empty")
        return v

    @field_validator("reason")
    @classmethod
    def reason_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("reason must not be empty")
        return v


# ─── Dossier: structured agent self-report (Phase H1) ────────────────────────


DossierState = Literal[
    "researching",
    "planning",
    "coding",
    "testing",
    "judging",
    "parked",
]


class PlanStep(BaseModel):
    step: str
    status: Literal["pending", "in_progress", "done"]
    notes: Optional[str] = None


class DossierOption(BaseModel):
    """One choice surfaced to the human when the agent parks needing a decision."""

    label: str
    description: str


AcceptanceKind = Literal["unit_test", "script", "http"]


class AcceptanceCheck(BaseModel):
    """One machine-verifiable acceptance criterion for a ticket.

    Produced by `ranch propose` and consumed by `run_acceptance` (H8). Each
    check is independently runnable. Browser + figma_diff are deliberately
    left out of v1 (need playwright + screenshot-diff infra); they layer on
    later without schema break.
    """

    kind: AcceptanceKind
    name: str  # human-readable label, e.g. "smoke test the /healthz endpoint"
    # Per-kind payload — kept loose; the judge module enforces shape per kind.
    cmd: Optional[str] = None  # unit_test + script
    pass_pattern: Optional[str] = None  # unit_test + script: substring or regex that proves pass
    url: Optional[str] = None  # http
    expected_status: Optional[int] = None  # http
    expected_body_contains: Optional[str] = None  # http
    timeout_seconds: float = 60.0

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be empty")
        return v


class RecordStateInput(BaseModel):
    """Payload the agent sends when calling mcp__ranch__record_state.

    The dossier is the agent's structured self-report of where it is right now.
    The console renders it as the primary view of the run (replacing the need
    to scroll the transcript). See ROADMAP Phase H1.

    `just_did` is the always-required one-liner shown in the collapsed view.
    `details` is the optional long-form expand-on-click narrative for the UI's
    Confluence-style stage entries (see #72) — what was attempted, results,
    decisions, issues, conclusions. Recommended when the step is non-trivial.
    """

    plan: list[PlanStep]
    just_did: str
    state: DossierState
    blocker: Optional[str] = None
    options: Optional[list[DossierOption]] = None
    files_touched: list[str] = []
    ticket: Optional[str] = None
    details: Optional[str] = None
    acceptance: Optional[list[AcceptanceCheck]] = None  # H8 — produced by propose, consumed by run_acceptance
    # H9 Phase 2 — agent's recommendation at pre_push about whether a staging deploy is needed
    recommended_action: Optional[Literal["deploy", "no_deploy", "needs_review"]] = None
    recommendation_reason: Optional[str] = None  # one-line rationale shown to the operator

    @field_validator("just_did")
    @classmethod
    def just_did_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("just_did must not be empty")
        return v


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
                "1. **Branch off the LATEST `origin/develop`** — never off main, "
                "your current HEAD, or whatever branch the worktree happened to be on:",
                "     git fetch origin develop",
                f"     git checkout -B {branch_hint} origin/develop",
                "2. Re-apply your changes if needed (the worktree base may differ "
                "from develop). Verify with `git diff origin/develop --stat` that "
                "ONLY your ticket's files appear.",
                "3. Stage your ticket's files. Run `git status` AFTER any auto-formatting "
                "(ruff/black/etc. un-stage files they modify — re-add them).",
                "4. Exclude unrelated files (migrations from other apps, lock files, etc.).",
                f"5. Commit: `{self.ticket}: <one-line summary>`" if self.ticket else "5. Commit with a clear message.",
                "6. Push to origin and open a PR with `bb pr create` (Bitbucket) "
                "or `gh pr create` (GitHub).",
            ]

        elif self.checkpoint_kind == "custom":
            lines.append("Approved. Continue.")

        return "\n".join(lines)


class HumanNote(BaseModel):
    """A mid-run human note forwarded to the agent, not tied to any checkpoint."""

    content: str

    def to_prompt(self) -> str:
        return f"[Human note mid-run]: {self.content}"


# ─── Browser verification (the verify stage / proto-Inspector) ───────────────


class CriterionVerdict(BaseModel):
    """One acceptance criterion, judged by ACTING in the browser."""

    criterion: str                    # the criterion text, verbatim
    passed: bool
    evidence: str                     # what the verifier DID and OBSERVED
    screenshot: Optional[str] = None  # filename saved in the artifacts dir

    @field_validator("evidence")
    @classmethod
    def evidence_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("evidence must not be empty")
        return v


class VerdictInput(BaseModel):
    """Payload the verifier sends when calling mcp__ranch__record_verdict.

    `summary` doubles as the feedback channel: on failure it must contain
    actionable fix guidance (expected vs actual, selectors, repro steps) —
    it becomes the brief for the fix-it development session.
    """

    overall_pass: bool
    criteria: list[CriterionVerdict]
    summary: str

    def to_fix_brief(self, url: str, artifacts_dir: str | None = None) -> str:
        """Render a failing verdict as the brief for a dev session."""
        failed = [c for c in self.criteria if not c.passed]
        lines = [
            "A browser-based verification of your work FAILED. Fix the issues below,",
            f"then stop. The app under test: {url}",
            "",
            "── FAILED CRITERIA ──",
        ]
        for c in failed:
            lines.append(f"✗ {c.criterion}")
            lines.append(f"    observed: {c.evidence}")
            if c.screenshot and artifacts_dir:
                lines.append(f"    screenshot: {artifacts_dir}/{c.screenshot}")
        lines += ["", "── VERIFIER'S SUMMARY ──", self.summary]
        return "\n".join(lines)
