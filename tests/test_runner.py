"""Tests for Phase 2 runner components. No real API calls — everything mocked."""
import asyncio
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from ranch.db import init_db, db_session
from ranch.models import Run, Checkpoint, Interjection
from ranch.runner.state import transition, VALID_TRANSITIONS, RUN_STATES


# ─── State machine ────────────────────────────────────────────

def test_state_transitions_legal():
    init_db()
    with db_session() as db:
        run = Run(agent="max", ticket="TEST-1", cwd="/tmp", initial_prompt="test", state="queued")
        db.add(run)
        db.flush()
        transition(run, "planning", session=db)
        assert run.state == "planning"
        transition(run, "in_development", session=db)
        assert run.state == "in_development"
        transition(run, "in_qa", session=db)
        assert run.state == "in_qa"
        transition(run, "completed", session=db)
        assert run.state == "completed"


def test_state_transitions_illegal():
    init_db()
    with db_session() as db:
        run = Run(agent="max", ticket="TEST-2", cwd="/tmp", initial_prompt="test", state="queued")
        db.add(run)
        db.flush()
        with pytest.raises(ValueError, match="Illegal state transition"):
            transition(run, "completed", session=db)


def test_needs_approval_saves_prior_state():
    init_db()
    with db_session() as db:
        run = Run(agent="max", ticket="TEST-3", cwd="/tmp", initial_prompt="test", state="planning")
        db.add(run)
        db.flush()
        transition(run, "needs_approval", session=db)
        assert run.state == "needs_approval"
        assert run.state_before_pause == "planning"


# ─── Checkpoint pause sets needs_approval ────────────────────

@pytest.mark.asyncio
async def test_checkpoint_pause_sets_needs_approval():
    init_db()
    with db_session() as db:
        run = Run(agent="max", ticket="TEST-4", cwd="/tmp", initial_prompt="test", state="planning")
        db.add(run)
        db.flush()
        run_id = run.id

    from ranch.runner.orchestrator import Orchestrator
    from pathlib import Path

    orch = Orchestrator("max", Path("/tmp"), "TEST-4", "test brief")
    orch.run_id = run_id

    await orch.on_checkpoint("plan_ready", "Here is the plan.", {"files": ["foo.py"]})

    with db_session() as db:
        run = db.query(Run).filter_by(id=run_id).one()
        assert run.state == "needs_approval"
        assert run.state_before_pause == "planning"
        cp = db.query(Checkpoint).filter_by(run_id=run_id).one()
        assert cp.kind == "plan_ready"
        assert cp.summary == "Here is the plan."

    assert orch._awaiting_approval is True


@pytest.mark.asyncio
async def test_checkpoint_tests_green_does_not_pause():
    """tests_green checkpoint should record but not require approval."""
    init_db()
    with db_session() as db:
        run = Run(agent="max", ticket="TEST-5", cwd="/tmp", initial_prompt="test", state="in_development")
        db.add(run)
        db.flush()
        run_id = run.id

    from ranch.runner.orchestrator import Orchestrator
    from pathlib import Path

    orch = Orchestrator("max", Path("/tmp"), "TEST-5", "test brief")
    orch.run_id = run_id

    await orch.on_checkpoint("tests_green", "All tests pass.", None)

    with db_session() as db:
        run = db.query(Run).filter_by(id=run_id).one()
        # state should NOT be needs_approval
        assert run.state == "in_development"

    assert orch._awaiting_approval is False


# ─── Interjection recording ───────────────────────────────────
# The stdin/CLI path enqueues Interjection rows; the DB poll loop dispatches
# them. These tests cover the dispatch step in isolation. Enqueue + poll →
# dispatch end-to-end is covered in test_interjections.py.

@pytest.mark.asyncio
async def test_interjection_stop_sets_flag():
    init_db()
    with db_session() as db:
        run = Run(agent="max", ticket="TEST-6", cwd="/tmp", initial_prompt="test", state="in_development")
        db.add(run)
        db.flush()
        run_id = run.id

    from ranch.runner.orchestrator import Orchestrator
    from pathlib import Path

    orch = Orchestrator("max", Path("/tmp"), "TEST-6", "test brief")
    orch.run_id = run_id

    mock_client = AsyncMock()
    await orch._dispatch_interjection("stop", "", mock_client)

    assert orch.stop_requested is True


@pytest.mark.asyncio
async def test_interjection_note_forwards_to_client():
    init_db()
    with db_session() as db:
        run = Run(agent="max", ticket="TEST-7", cwd="/tmp", initial_prompt="test", state="in_development")
        db.add(run)
        db.flush()
        run_id = run.id

    from ranch.runner.orchestrator import Orchestrator
    from pathlib import Path

    orch = Orchestrator("max", Path("/tmp"), "TEST-7", "test brief")
    orch.run_id = run_id

    mock_client = AsyncMock()
    await orch._dispatch_interjection("note", "check the edge case", mock_client)

    mock_client.query.assert_called_once()
    call_arg = mock_client.query.call_args[0][0]
    assert "check the edge case" in call_arg


# ─── Finalize writes ended_at ─────────────────────────────────

@pytest.mark.asyncio
async def test_finalize_writes_ended_at():
    init_db()
    with db_session() as db:
        run = Run(agent="max", ticket="TEST-8", cwd="/tmp", initial_prompt="test", state="in_development")
        db.add(run)
        db.flush()
        run_id = run.id

    from ranch.runner.orchestrator import Orchestrator
    from pathlib import Path

    orch = Orchestrator("max", Path("/tmp"), "TEST-8", "test brief")
    orch.run_id = run_id
    orch.ticket = None  # skip reflection subprocess

    await orch._finalize()

    with db_session() as db:
        run = db.query(Run).filter_by(id=run_id).one()
        assert run.ended_at is not None
        assert run.exit_reason == "completed"
        assert run.state == "completed"
