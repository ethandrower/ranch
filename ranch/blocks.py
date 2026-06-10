"""Block construct — cross-ticket cascading-dependency management.

A Run can be `blocked` by another ticket's decision. While blocked, the
hand scheduler skips it. The block auto-resolves when the blocker ticket
gets a checkpoint approval (operator action), or when the operator runs
`ranch unblock <run_id>`.

This module is the only place that writes to / queries the Block table —
keep it that way so the propagation semantics stay clear.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import db_session
from .models import Block, Checkpoint, Run


# ─── Writes ────────────────────────────────────────────────────────


def record_block(
    *,
    blocked_run_id: int,
    blocker_ticket: str,
    reason: str,
    source: str = "agent",
) -> Block:
    """Write a Block row. Source = "agent" (via record_block MCP tool) or
    "operator" (via `ranch block` CLI).

    Idempotent: if an unresolved block with the same (blocked_run_id,
    blocker_ticket) pair already exists, returns it instead of creating a
    duplicate. Reason field is updated in-place if the new reason differs.
    """
    with db_session() as session:
        existing = (
            session.execute(
                select(Block)
                .where(Block.blocked_run_id == blocked_run_id)
                .where(Block.blocker_ticket == blocker_ticket)
                .where(Block.resolved_at.is_(None))
            )
            .scalars()
            .first()
        )
        if existing is not None:
            if existing.reason != reason:
                existing.reason = reason
            session.flush()
            session.refresh(existing)
            session.expunge(existing)
            return existing

        # Best-effort: try to link blocker_run_id by looking up an active
        # run with the same ticket. Many of these will be None at write
        # time — that's fine; resolution joins on blocker_ticket anyway.
        blocker_run_id = (
            session.execute(
                select(Run.id)
                .where(Run.ticket == blocker_ticket)
                .where(Run.ended_at.is_(None))
                .order_by(Run.started_at.desc())
                .limit(1)
            )
            .scalar()
        )

        block = Block(
            blocked_run_id=blocked_run_id,
            blocker_run_id=blocker_run_id,
            blocker_ticket=blocker_ticket,
            reason=reason,
            source=source,
        )
        session.add(block)
        session.flush()
        session.refresh(block)
        session.expunge(block)
        return block


# ─── Reads ─────────────────────────────────────────────────────────


def is_run_blocked(run_id: int) -> bool:
    """True if this run has at least one unresolved block against it.

    Used by the hand scheduler to skip blocked runs.
    """
    with db_session() as session:
        return _is_run_blocked_in_session(session, run_id)


def _is_run_blocked_in_session(session: Session, run_id: int) -> bool:
    return (
        session.execute(
            select(Block.id)
            .where(Block.blocked_run_id == run_id)
            .where(Block.resolved_at.is_(None))
            .limit(1)
        )
        .first()
        is not None
    )


def open_blocks_for_ticket(blocker_ticket: str) -> list[Block]:
    """All unresolved blocks where this ticket is the blocker. Used at
    resolution time to find blocked runs to free."""
    with db_session() as session:
        rows = (
            session.execute(
                select(Block)
                .where(Block.blocker_ticket == blocker_ticket)
                .where(Block.resolved_at.is_(None))
            )
            .scalars()
            .all()
        )
        for r in rows:
            session.expunge(r)
        return rows


# ─── Resolution ────────────────────────────────────────────────────


def resolve_blocks_for_ticket(
    blocker_ticket: str,
    *,
    by_checkpoint_id: Optional[int] = None,
) -> int:
    """Mark all open blocks against `blocker_ticket` as resolved.

    Called when the blocker ticket gets a checkpoint approval — typically
    from the CLI's `approve` command or from the operator's `ranch unblock`.

    Returns the number of blocks resolved.
    """
    if not blocker_ticket:
        return 0
    with db_session() as session:
        now = datetime.now(timezone.utc)
        result = (
            session.query(Block)
            .filter(Block.blocker_ticket == blocker_ticket)
            .filter(Block.resolved_at.is_(None))
            .update(
                {
                    Block.resolved_at: now,
                    Block.resolved_by_checkpoint_id: by_checkpoint_id,
                },
                synchronize_session=False,
            )
        )
        return int(result or 0)


def resolve_blocks_for_run(run_id: int) -> int:
    """Mark all open blocks against a specific run id as resolved (operator
    override path)."""
    with db_session() as session:
        now = datetime.now(timezone.utc)
        result = (
            session.query(Block)
            .filter(Block.blocked_run_id == run_id)
            .filter(Block.resolved_at.is_(None))
            .update({Block.resolved_at: now}, synchronize_session=False)
        )
        return int(result or 0)


# ─── Approve-side integration helper ───────────────────────────────


def resolve_blocks_on_approve(run_id: int, checkpoint_id: Optional[int] = None) -> int:
    """Convenience: look up the run's ticket, then resolve any open blocks
    where that ticket is the blocker. Hooked into the CLI's `approve`
    command so plain operator approval propagates automatically.

    No-op if the run has no ticket (adhoc runs can't be blockers).
    """
    with db_session() as session:
        ticket = session.execute(
            select(Run.ticket).where(Run.id == run_id)
        ).scalar()
    if not ticket:
        return 0
    return resolve_blocks_for_ticket(ticket, by_checkpoint_id=checkpoint_id)
