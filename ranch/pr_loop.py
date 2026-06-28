"""H20 — PR review polling + response, extracted for reuse.

Both `ranch poll-pr` / `ranch respond-pr` CLI commands AND the
`RanchHand` daemon call into these. Keeps the logic in one place so the
hand's auto-poll behavior cannot drift from the manual CLI behavior.

The hand uses `runs_pending_pr_review` to find work, calls
`poll_pr_for_run` to fetch new comments, and on a positive result fires
`respond_to_pr_review` to wake the agent.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .db import db_session
from .models import Dossier, ReviewComment, Run
from .runner.pr_backend import (
    PRBackendError,
    detect_platform,
    get_backend,
)


TERMINAL_RUN_STATES = {"completed", "stopped", "error"}


# ─── Result type ──────────────────────────────────────────────────


@dataclass
class PollResult:
    """Outcome of one `poll_pr_for_run` call."""

    ok: bool
    pr_id: Optional[str] = None
    new_comment_count: int = 0
    new_comments: list[ReviewComment] = field(default_factory=list)
    reason: Optional[str] = None  # populated when ok=False


# ─── Discovery + polling ──────────────────────────────────────────


def poll_pr_for_run(
    run_id: int,
    *,
    pr_override: Optional[str] = None,
    platform_override: Optional[str] = None,
) -> PollResult:
    """Fetch new PR review comments for a run; persist them as ReviewComment rows.

    Re-running is idempotent — comments already stored (matched by
    platform_comment_id) are skipped. Discovery (when the run has no
    pr_id yet) is best-effort: returns ok=True with new_comment_count=0
    when no PR exists yet, so the hand's polling loop can keep retrying
    without erroring out on freshly-pushed branches.

    Updates Run.last_pr_check_at so the hand can throttle subsequent
    polls per the cadence policy.
    """
    with db_session() as db:
        run = db.query(Run).filter_by(id=run_id).one_or_none()
        if not run:
            return PollResult(ok=False, reason=f"run #{run_id} not found")
        branch = run.branch_name
        cwd = Path(run.cwd) if run.cwd else None
        pr_id = pr_override or run.pr_id
        pr_platform = (
            platform_override
            or run.pr_platform
            or (detect_platform(cwd) if cwd else None)
        )

    if not cwd:
        return PollResult(ok=False, reason=f"run #{run_id} has no cwd")
    # Fail fast on no-id-AND-no-branch before bothering with platform
    # detection — the operator just hasn't set anything up for this run yet.
    if not pr_id and not branch:
        return PollResult(ok=False, reason=f"run #{run_id} has no branch_name — cannot auto-discover PR")
    if not pr_platform:
        return PollResult(ok=False, reason="could not detect PR platform — pass --platform")

    backend = get_backend(pr_platform)

    # Discover PR if missing
    if not pr_id:
        if not branch:
            return PollResult(ok=False, reason=f"run #{run_id} has no branch_name — cannot auto-discover PR")
        try:
            found = backend.discover_pr_by_branch(branch, cwd)
        except PRBackendError as e:
            return PollResult(ok=False, reason=f"PR discovery failed: {e}")
        if not found:
            # Loop-friendly: quiet success when no PR exists yet
            _touch_last_check(run_id)
            return PollResult(ok=True, pr_id=None, new_comment_count=0)
        pr_id, pr_url = found
        ticket_key = None
        with db_session() as db:
            run_row = db.query(Run).filter_by(id=run_id).one_or_none()
            ticket_key = run_row.ticket if run_row else None
            db.query(Run).filter_by(id=run_id).update({
                "pr_id": pr_id,
                "pr_platform": pr_platform,
                "pr_url": pr_url,
            })
        # Jira sync: a PR just opened for this ticket → move it to the review
        # status so the board reflects "in review" without anyone updating Jira
        # by hand. "Pending Approval" is the citemed workflow's review status
        # (operator-configurable mapping). Best-effort — a Jira hiccup must
        # never break the PR loop.
        if ticket_key:
            try:
                from .jira_backend import resolve_jira_client
                client, _ = resolve_jira_client()
                with client:
                    new_status = client.transition(
                        ticket_key, "Pending Approval",
                        comment=f"Ranch: PR opened — {pr_url}" if pr_url else None,
                    )
                print(f"[ranch.pr_loop] jira {ticket_key} → {new_status} (PR opened)")
            except Exception as e:
                print(f"[ranch.pr_loop] jira transition on PR open failed for {ticket_key}: {e}")

    # Fetch + dedupe
    try:
        fetched = backend.fetch_comments(pr_id, cwd)
    except PRBackendError as e:
        return PollResult(ok=False, pr_id=pr_id, reason=f"comment fetch failed: {e}")

    new_rows: list[ReviewComment] = []
    with db_session() as db:
        existing = {
            pcid
            for (pcid,) in db.query(ReviewComment.platform_comment_id)
            .filter_by(run_id=run_id)
            .all()
        }
        for c in fetched:
            if c.platform_comment_id in existing:
                continue
            row = ReviewComment(
                run_id=run_id,
                platform_comment_id=c.platform_comment_id,
                author=c.author,
                file_path=c.file_path,
                line_number=c.line_number,
                body=c.body,
                created_at_remote=c.created_at_remote,
            )
            db.add(row)
            new_rows.append(row)

    _touch_last_check(run_id)
    return PollResult(
        ok=True, pr_id=pr_id,
        new_comment_count=len(new_rows),
        new_comments=new_rows,
    )


def _touch_last_check(run_id: int) -> None:
    with db_session() as db:
        db.query(Run).filter_by(id=run_id).update({
            "last_pr_check_at": datetime.now(timezone.utc),
        })


# ─── Finding runs that need a poll ────────────────────────────────


@dataclass
class PRPollCandidate:
    """A Run the hand should consider polling for PR review unblocks."""

    run_id: int
    agent: str
    ticket: Optional[str]
    pr_id: Optional[str]
    pr_platform: Optional[str]
    branch_name: Optional[str]
    last_check_at: Optional[datetime]


def runs_pending_pr_review(
    agent: Optional[str] = None,
    *,
    require_parked_dossier: bool = True,
) -> list[PRPollCandidate]:
    """Return Runs that are at the "post-push, awaiting review" stage.

    Filters:
      - Terminal Run.state (the agent's work session has ended)
      - Has either pr_id set OR a branch_name (so discovery can still work)
      - If `require_parked_dossier`: latest dossier is `parked` (default —
        excludes runs that never reached pre_push approval)
      - Scoped by agent name if provided

    The hand calls this every poll cycle and then applies the cadence
    throttle (last_pr_check_at) before deciding which to actually poll.
    """
    with db_session() as db:
        q = db.query(Run).filter(Run.state.in_(TERMINAL_RUN_STATES))
        if agent:
            q = q.filter(Run.agent == agent)
        # Must have either a pr_id (already discovered) or branch_name
        # (still need discovery — covers freshly-pushed branches).
        q = q.filter((Run.pr_id.isnot(None)) | (Run.branch_name.isnot(None)))
        runs = q.order_by(Run.ended_at.desc()).all()

        out: list[PRPollCandidate] = []
        for run in runs:
            if require_parked_dossier:
                latest = (
                    db.query(Dossier)
                    .filter_by(run_id=run.id)
                    .order_by(Dossier.created_at.desc())
                    .first()
                )
                if not latest or latest.state != "parked":
                    continue
            # SQLite returns naive datetimes; normalize to UTC-aware so
            # cadence comparisons work without TypeError. The stored values
            # are always UTC because _touch_last_check uses now(utc).
            last = run.last_pr_check_at
            if last is not None and last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            out.append(PRPollCandidate(
                run_id=run.id,
                agent=run.agent,
                ticket=run.ticket,
                pr_id=run.pr_id,
                pr_platform=run.pr_platform,
                branch_name=run.branch_name,
                last_check_at=last,
            ))
        return out


def filter_by_poll_cadence(
    candidates: list[PRPollCandidate],
    *,
    interval_seconds: float,
    now: Optional[datetime] = None,
) -> list[PRPollCandidate]:
    """Drop candidates that have been polled too recently per the cadence."""
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - _td(seconds=interval_seconds)
    out: list[PRPollCandidate] = []
    for c in candidates:
        if c.last_check_at is None or c.last_check_at < cutoff:
            out.append(c)
    return out


def _td(*, seconds: float):
    """Small helper so the import line for timedelta doesn't leak into hand callers."""
    from datetime import timedelta
    return timedelta(seconds=seconds)


# ─── Wake the agent to respond to comments ────────────────────────


async def respond_to_pr_review(
    run_id: int,
    *,
    budget_seconds: Optional[float] = None,
) -> None:
    """Resume the agent with pending PR review comments as the brief.

    Continues the same SDK conversation via the stored sdk_session_id +
    SYSTEM_PROMPT_PR_REVIEW. The agent runs the TRIAGE → FIX → PRE-PUSH
    workflow already specified in prompts.py.

    Used by both `ranch respond-pr` (manual) and the hand's auto-respond
    path (H20).
    """
    from claude_code_sdk import ClaudeCodeOptions, ClaudeSDKClient

    from .runner.checkpoints import make_checkpoint_hook
    from .runner.dossier import make_dossier_hook
    from .runner.judge_hook import make_judge_hook
    from .runner.orchestrator import Orchestrator
    from .runner.prompts import (
        SYSTEM_PROMPT_PR_REVIEW,
        pr_review_initial_prompt,
    )
    from .runner.tools import ranch_mcp, reset_judge_budget

    with db_session() as db:
        run = db.query(Run).filter_by(id=run_id).one_or_none()
        if not run:
            raise ValueError(f"run #{run_id} not found")
        if not run.pr_id:
            raise ValueError(f"run #{run_id} has no PR attached")
        if not run.sdk_session_id:
            raise ValueError(f"run #{run_id} has no SDK session — cannot resume")
        agent = run.agent
        ticket = run.ticket or ""
        pr_id = run.pr_id
        pr_platform = run.pr_platform or "bb"
        cwd = Path(run.cwd)
        sdk_session_id = run.sdk_session_id

        pending = (
            db.query(ReviewComment)
            .filter_by(run_id=run_id, resolved=0)
            .order_by(ReviewComment.id)
            .all()
        )
        comment_dicts = [
            {
                "platform_comment_id": c.platform_comment_id,
                "author": c.author,
                "file_path": c.file_path,
                "line_number": c.line_number,
                "body": c.body,
            }
            for c in pending
        ]

    if not comment_dicts:
        return  # nothing to respond to

    brief = pr_review_initial_prompt(ticket, pr_id, pr_platform, comment_dicts)

    orch = Orchestrator(
        agent=agent, cwd=cwd, ticket=ticket, brief=brief,
        budget_seconds=budget_seconds,
    )
    orch.run_id = run_id
    reset_judge_budget()

    options = ClaudeCodeOptions(
        cwd=str(cwd),
        append_system_prompt=SYSTEM_PROMPT_PR_REVIEW,
        mcp_servers={"ranch": ranch_mcp},
        allowed_tools=[
            "Read", "Write", "Edit", "Bash", "Grep", "Glob",
            "mcp__ranch__record_checkpoint", "mcp__ranch__log_decision",
            "mcp__ranch__record_state", "mcp__ranch__run_acceptance",
        ],
        hooks={"PostToolUse": [
            make_checkpoint_hook(orch),
            make_dossier_hook(orch),
            make_judge_hook(orch),
        ]},
        permission_mode="acceptEdits",
        resume=sdk_session_id,
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query(brief)
        stdin_task = None
        poll_task = None
        budget_task = None
        if orch.budget_seconds is not None:
            budget_task = asyncio.create_task(orch._budget_watchdog())
        try:
            await orch._main_loop(client)
        finally:
            for t in (stdin_task, poll_task, budget_task):
                if t is not None:
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass

    await orch._finalize()
