"""Runtime helpers for Plan C — process liveness, orphan reaping, state polling.

Kept separate from cli.py so the logic is testable without a click runner.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from .db import db_session
from .models import Run


TERMINAL_STATES = {"completed", "stopped", "error"}


def is_alive(pid: int | None) -> bool:
    """Return True if a process with this PID exists and we can signal it.

    Uses the `kill(pid, 0)` trick — sends no signal, just checks existence.
    A PermissionError means the process exists but is owned by another user,
    which still counts as alive for our purposes.
    """
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def mark_orphans() -> list[int]:
    """Find dispatched runs whose PID is dead but state is non-terminal.

    Marks each as state='error', exit_reason='orphaned'. Returns the list of
    run IDs that were reaped. Idempotent — a second call is a no-op.
    """
    reaped: list[int] = []
    with db_session() as db:
        candidates = (
            db.query(Run)
            .filter(
                Run.dispatch_mode == "background",
                Run.pid.is_not(None),
                ~Run.state.in_(TERMINAL_STATES),
            )
            .all()
        )
        now = datetime.now(timezone.utc)
        for run in candidates:
            if not is_alive(run.pid):
                run.state = "error"
                run.exit_reason = "orphaned"
                run.ended_at = now
                reaped.append(run.id)
    return reaped


def snapshot_states(run_ids: list[int] | None = None) -> dict[int, str]:
    """Return {run_id: state} for the given runs (or all non-terminal if None)."""
    with db_session() as db:
        q = db.query(Run.id, Run.state)
        if run_ids is not None:
            q = q.filter(Run.id.in_(run_ids))
        else:
            q = q.filter(~Run.state.in_(TERMINAL_STATES))
        return {rid: state for rid, state in q.all()}


def watch_for_change(
    run_ids: list[int] | None = None,
    timeout_seconds: float | None = None,
    poll_interval: float = 0.5,
    clock=time.monotonic,
    sleep=time.sleep,
) -> tuple[int, str] | None:
    """Block until any watched run's state changes, or timeout expires.

    Returns (run_id, new_state) on transition, or None if the timeout fires
    before anything changed. `clock` and `sleep` are injectable for tests.
    """
    mark_orphans()
    initial = snapshot_states(run_ids)
    deadline = None if timeout_seconds is None else clock() + timeout_seconds

    while True:
        if deadline is not None and clock() >= deadline:
            return None
        sleep(poll_interval)
        mark_orphans()
        current = snapshot_states(run_ids)

        # Detect any change: state differs, or a run disappeared (transitioned
        # to terminal + filtered out when run_ids is None).
        watched = set(initial) | set(current)
        for rid in watched:
            prev = initial.get(rid)
            now = current.get(rid)
            if prev != now:
                # For disappearances (terminal transition), refetch the real state
                if now is None:
                    with db_session() as db:
                        row = db.query(Run).filter_by(id=rid).one_or_none()
                        if row is not None:
                            now = row.state
                return rid, now or "unknown"
        initial = current
