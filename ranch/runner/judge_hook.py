"""H8 — PostToolUse hook that runs the acceptance checks for `run_acceptance`.

The MCP tool body (tools.py) just ticks the budget and returns a placeholder.
This hook is what actually executes the checks in the worktree, then injects
the structured results back to the agent as `additionalContext` on the same
tool result — so the agent's next turn sees the pass/fail breakdown attached
to its own tool call (same trick as the checkpoint hook).
"""
from __future__ import annotations

import json
from typing import Iterable

from claude_code_sdk import HookMatcher
from claude_code_sdk.types import HookContext
from pydantic import ValidationError

from ranch.db import db_session
from ranch.judge import run_acceptance as _run_acceptance
from ranch.models import Dossier
from ranch.runner.messages import AcceptanceCheck

RUN_ACCEPTANCE_TOOL = "mcp__ranch__run_acceptance"


def _latest_acceptance_from_dossier(run_id: int | None) -> list[AcceptanceCheck]:
    """Pull the most-recent acceptance list off this run's dossier history."""
    if run_id is None:
        return []
    with db_session() as db:
        rows = (
            db.query(Dossier)
            .filter_by(run_id=run_id)
            .order_by(Dossier.created_at.desc())
            .all()
        )
        for row in rows:
            try:
                payload = json.loads(row.payload_json)
            except (json.JSONDecodeError, TypeError):
                continue
            acceptance = payload.get("acceptance")
            if acceptance:
                try:
                    return [AcceptanceCheck.model_validate(c) for c in acceptance]
                except ValidationError:
                    continue
    return []


def _format_results_for_agent(judge_run, source: str) -> str:
    """Render the JudgeRun as plain text the agent can read + react to."""
    if not judge_run.results:
        return (
            "ACCEPTANCE RUN — no checks executed.\n"
            "No `acceptance` list was found in this run's dossier and none were "
            "passed inline. If you intended to run inline checks, pass them as "
            "the `checks` argument. If propose was supposed to set acceptance, "
            "the dossier missed it — record_state with the acceptance field now."
        )

    header = (
        f"ACCEPTANCE RUN — source: {source}\n"
        f"{'PASS' if judge_run.all_passed else 'FAIL'} "
        f"({len(judge_run.results) - judge_run.num_failed}/{len(judge_run.results)} passed)\n"
    )
    lines = [header]
    for r in judge_run.results:
        lines.append(r.summary_line())
        if not r.passed:
            if r.error:
                lines.append(f"      error: {r.error}")
            if r.output:
                # Indent the output so it's visually clear it belongs to this check
                indented = "\n".join("      " + ln for ln in r.output.splitlines()[:30])
                lines.append(indented)

    if judge_run.all_passed:
        lines.append("\nAll checks passed. You may proceed to pre_push or park.")
    else:
        lines.append(
            "\nOne or more checks failed. Read the output above, fix the underlying "
            "issue (edit code, restart a server, etc.), and call run_acceptance again "
            "to re-verify. Don't park until checks pass or your judge budget is exhausted."
        )
    return "\n".join(lines)


def make_judge_hook(orchestrator) -> HookMatcher:
    """Return a PostToolUse HookMatcher for run_acceptance."""

    async def on_post_tool_use(
        input_data: dict,
        tool_use_id: str | None,
        context: HookContext,
    ) -> dict:
        tool_name = input_data.get("tool_name", "")
        if tool_name != RUN_ACCEPTANCE_TOOL:
            return {}

        tool_input = input_data.get("tool_input") or {}
        inline_checks_raw: Iterable[dict] = tool_input.get("checks") or []
        cwd_override = tool_input.get("cwd")
        cwd = orchestrator.cwd if not cwd_override else type(orchestrator.cwd)(cwd_override)

        # Resolve which checks to run
        checks: list[AcceptanceCheck] = []
        source: str
        if inline_checks_raw:
            try:
                checks = [AcceptanceCheck.model_validate(c) for c in inline_checks_raw]
                source = f"inline ({len(checks)} check{'s' if len(checks) != 1 else ''})"
            except ValidationError as exc:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": (
                            f"run_acceptance: inline `checks` failed validation:\n{exc}"
                        ),
                    }
                }
        else:
            checks = _latest_acceptance_from_dossier(orchestrator.run_id)
            source = "dossier (from propose)" if checks else "none-found"

        judge_run = _run_acceptance(checks, cwd)
        report = _format_results_for_agent(judge_run, source)

        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": report,
            }
        }

    return HookMatcher(matcher=RUN_ACCEPTANCE_TOOL, hooks=[on_post_tool_use])
