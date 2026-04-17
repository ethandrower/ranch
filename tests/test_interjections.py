"""Tests for out-of-process interjection: DB poll loop + CLI commands.

Covers Plan A from the roadmap (Issue #11):
- `ranch approve/reject/note/stop <run_id>` insert Interjection rows
- Orchestrator's DB poll loop consumes pending rows and dispatches them
- processed_at marker prevents re-dispatching
- Stdin path still works (enqueues rows, poller dispatches)
- Runs in terminal state warn but still accept writes (no-op consumption)
"""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from ranch.cli import cli
from ranch.db import db_session, init_db
from ranch.models import Interjection, Run
from ranch.runner.orchestrator import Orchestrator


def _make_run(state: str = "in_development", ticket: str = "TEST-INT") -> int:
    init_db()
    with db_session() as db:
        run = Run(agent="max", ticket=ticket, cwd="/tmp", initial_prompt="brief", state=state)
        db.add(run)
        db.flush()
        return run.id


def _pending_count(run_id: int) -> int:
    with db_session() as db:
        return (
            db.query(Interjection)
            .filter_by(run_id=run_id, processed_at=None)
            .count()
        )


# ─── CLI commands insert rows ──────────────────────────────────────


def test_cli_approve_inserts_approve_row():
    run_id = _make_run()
    result = CliRunner().invoke(cli, ["approve", str(run_id)])
    assert result.exit_code == 0
    with db_session() as db:
        rows = db.query(Interjection).filter_by(run_id=run_id).all()
        assert len(rows) == 1
        assert rows[0].kind == "approve"
        assert rows[0].processed_at is None


def test_cli_approve_with_note():
    run_id = _make_run()
    result = CliRunner().invoke(cli, ["approve", str(run_id), "--note", "looks good"])
    assert result.exit_code == 0
    with db_session() as db:
        row = db.query(Interjection).filter_by(run_id=run_id).one()
        assert row.kind == "approve"
        assert row.content == "looks good"


def test_cli_reject_with_reason():
    run_id = _make_run()
    result = CliRunner().invoke(cli, ["reject", str(run_id), "scope too wide"])
    assert result.exit_code == 0
    with db_session() as db:
        row = db.query(Interjection).filter_by(run_id=run_id).one()
        assert row.kind == "reject"
        assert row.content == "scope too wide"


def test_cli_note_multi_word():
    run_id = _make_run()
    result = CliRunner().invoke(cli, ["note", str(run_id), "please", "check", "edge", "cases"])
    assert result.exit_code == 0
    with db_session() as db:
        row = db.query(Interjection).filter_by(run_id=run_id).one()
        assert row.kind == "note"
        assert row.content == "please check edge cases"


def test_cli_stop_inserts_stop_row():
    run_id = _make_run()
    result = CliRunner().invoke(cli, ["stop", str(run_id)])
    assert result.exit_code == 0
    with db_session() as db:
        row = db.query(Interjection).filter_by(run_id=run_id).one()
        assert row.kind == "stop"


def test_cli_unknown_run_aborts():
    init_db()
    result = CliRunner().invoke(cli, ["approve", "99999"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_cli_terminal_state_still_writes_but_warns():
    """A run that's already completed shouldn't crash the CLI — we just warn
    and write the row. It'll never be consumed."""
    run_id = _make_run(state="completed")
    result = CliRunner().invoke(cli, ["approve", str(run_id)])
    assert result.exit_code == 0
    assert "completed" in result.output.lower()
    with db_session() as db:
        assert db.query(Interjection).filter_by(run_id=run_id).count() == 1


# ─── DB poll loop dispatches rows ──────────────────────────────────


@pytest.mark.asyncio
async def test_poll_loop_dispatches_approve():
    """A pending approve row fires the approval event and is marked processed."""
    run_id = _make_run()
    orch = Orchestrator("max", Path("/tmp"), "TEST-INT", "brief")
    orch.run_id = run_id

    with db_session() as db:
        db.add(Interjection(run_id=run_id, kind="approve", content=""))

    mock_client = MagicMock()
    task = asyncio.create_task(orch._db_poll_loop(mock_client))
    try:
        await asyncio.wait_for(orch._approval_ready.wait(), timeout=2.0)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert orch._approval_result == "approved"
    assert _pending_count(run_id) == 0  # row was marked processed


@pytest.mark.asyncio
async def test_poll_loop_dispatches_reject_with_reason():
    run_id = _make_run()
    orch = Orchestrator("max", Path("/tmp"), "TEST-INT", "brief")
    orch.run_id = run_id

    with db_session() as db:
        db.add(Interjection(run_id=run_id, kind="reject", content="needs rework"))

    mock_client = MagicMock()
    task = asyncio.create_task(orch._db_poll_loop(mock_client))
    try:
        await asyncio.wait_for(orch._approval_ready.wait(), timeout=2.0)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert "rejected" in orch._approval_result
    assert "needs rework" in orch._approval_result


@pytest.mark.asyncio
async def test_poll_loop_dispatches_stop_sets_flag():
    run_id = _make_run()
    orch = Orchestrator("max", Path("/tmp"), "TEST-INT", "brief")
    orch.run_id = run_id

    with db_session() as db:
        db.add(Interjection(run_id=run_id, kind="stop", content=""))

    mock_client = MagicMock()
    task = asyncio.create_task(orch._db_poll_loop(mock_client))
    # Stop sets stop_requested, which causes the poll loop to exit itself
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert orch.stop_requested is True
    assert orch._approval_result == "stopped"


@pytest.mark.asyncio
async def test_poll_loop_dispatches_note_via_client_query():
    run_id = _make_run()
    orch = Orchestrator("max", Path("/tmp"), "TEST-INT", "brief")
    orch.run_id = run_id

    with db_session() as db:
        db.add(Interjection(run_id=run_id, kind="note", content="remember the cache"))

    mock_client = MagicMock()
    mock_client.query = AsyncMock()
    task = asyncio.create_task(orch._db_poll_loop(mock_client))
    try:
        # Wait for the note to be dispatched — poll cadence is 500ms
        for _ in range(10):
            await asyncio.sleep(0.3)
            if mock_client.query.await_count > 0:
                break
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    mock_client.query.assert_awaited_once()
    arg = mock_client.query.await_args[0][0]
    assert "remember the cache" in arg


@pytest.mark.asyncio
async def test_poll_loop_skips_already_processed_rows():
    """A row with processed_at set is never re-dispatched."""
    from datetime import datetime, timezone

    run_id = _make_run()
    with db_session() as db:
        db.add(Interjection(
            run_id=run_id,
            kind="approve",
            content="",
            processed_at=datetime.now(timezone.utc),
        ))

    orch = Orchestrator("max", Path("/tmp"), "TEST-INT", "brief")
    orch.run_id = run_id
    mock_client = MagicMock()
    task = asyncio.create_task(orch._db_poll_loop(mock_client))
    await asyncio.sleep(1.0)  # give it two full poll cycles
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert orch._approval_result is None
    assert not orch._approval_ready.is_set()


@pytest.mark.asyncio
async def test_poll_loop_ignores_other_runs():
    """Rows belonging to another run are not dispatched."""
    run_id = _make_run(ticket="TEST-A")
    other_run_id = _make_run(ticket="TEST-B")

    with db_session() as db:
        db.add(Interjection(run_id=other_run_id, kind="approve", content=""))

    orch = Orchestrator("max", Path("/tmp"), "TEST-A", "brief")
    orch.run_id = run_id
    mock_client = MagicMock()
    task = asyncio.create_task(orch._db_poll_loop(mock_client))
    await asyncio.sleep(1.0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert orch._approval_result is None
    # The other run's row stays pending
    assert _pending_count(other_run_id) == 1


@pytest.mark.asyncio
async def test_double_approve_is_idempotent():
    """Two approve rows both get marked processed; only one effective state change."""
    run_id = _make_run()
    orch = Orchestrator("max", Path("/tmp"), "TEST-INT", "brief")
    orch.run_id = run_id

    with db_session() as db:
        db.add(Interjection(run_id=run_id, kind="approve", content=""))
        db.add(Interjection(run_id=run_id, kind="approve", content=""))

    mock_client = MagicMock()
    task = asyncio.create_task(orch._db_poll_loop(mock_client))
    try:
        await asyncio.wait_for(orch._approval_ready.wait(), timeout=2.0)
        await asyncio.sleep(0.7)  # let poll cycle pick up both rows
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert orch._approval_result == "approved"
    assert _pending_count(run_id) == 0


# ─── Stdin path ────────────────────────────────────────────────────


def test_enqueue_interjection_inserts_pending_row():
    """Stdin path writes rows via _enqueue_interjection with processed_at NULL."""
    run_id = _make_run()
    orch = Orchestrator("max", Path("/tmp"), "TEST-INT", "brief")
    orch.run_id = run_id

    orch._enqueue_interjection("approve", "")

    with db_session() as db:
        row = db.query(Interjection).filter_by(run_id=run_id).one()
        assert row.kind == "approve"
        assert row.processed_at is None
