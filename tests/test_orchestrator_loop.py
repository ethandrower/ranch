"""Tests for Orchestrator._main_loop and related helpers.

Focuses on:
- rate_limit_event retry logic (new code)
- approval checkpoint flow
- _record_decision DB updates
- make_checkpoint_hook behaviour
- initial_user_prompt construction
"""
import asyncio
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from claude_code_sdk._errors import MessageParseError
from claude_code_sdk.types import ResultMessage, SystemMessage

from ranch.db import init_db, db_session
from ranch.models import Run, Checkpoint
from ranch.runner.orchestrator import Orchestrator


# ─── Helpers ────────────────────────────────────────────────────


def _result_msg():
    return ResultMessage(
        subtype="success",
        duration_ms=100,
        duration_api_ms=90,
        is_error=False,
        num_turns=1,
        session_id="sess-test",
        total_cost_usd=0.001,
        usage={},
        result="ok",
    )


async def _async_gen(*items):
    """Yield items from an async generator."""
    for item in items:
        yield item


async def _async_gen_raising(error):
    """Async generator that raises immediately."""
    raise error
    yield  # makes this an async generator


def _make_run(state="planning", ticket="TEST-LOOP") -> int:
    init_db()
    with db_session() as db:
        run = Run(agent="max", ticket=ticket, cwd="/tmp", initial_prompt="brief", state=state)
        db.add(run)
        db.flush()
        return run.id


# ─── rate_limit_event retry ──────────────────────────────────────


@pytest.mark.asyncio
async def test_main_loop_retries_on_rate_limit_event():
    """MessageParseError for rate_limit_event is caught and the loop retries."""
    run_id = _make_run(ticket="TEST-RL1")
    orch = Orchestrator("max", Path("/tmp"), "TEST-RL1", "brief")
    orch.run_id = run_id

    mock_client = MagicMock()
    mock_client.receive_response = MagicMock(side_effect=[
        _async_gen_raising(MessageParseError("Unknown message type: rate_limit_event", {})),
        _async_gen(_result_msg()),
    ])

    await orch._main_loop(mock_client)

    assert mock_client.receive_response.call_count == 2


@pytest.mark.asyncio
async def test_main_loop_multiple_rate_limit_retries():
    """Multiple consecutive rate_limit_events are all retried correctly."""
    run_id = _make_run(ticket="TEST-RL2")
    orch = Orchestrator("max", Path("/tmp"), "TEST-RL2", "brief")
    orch.run_id = run_id

    mock_client = MagicMock()
    mock_client.receive_response = MagicMock(side_effect=[
        _async_gen_raising(MessageParseError("Unknown message type: rate_limit_event", {})),
        _async_gen_raising(MessageParseError("Unknown message type: rate_limit_event", {})),
        _async_gen(_result_msg()),
    ])

    await orch._main_loop(mock_client)

    assert mock_client.receive_response.call_count == 3


@pytest.mark.asyncio
async def test_main_loop_non_rate_limit_parse_error_propagates():
    """MessageParseError for an unknown (non-rate-limit) type re-raises."""
    run_id = _make_run(ticket="TEST-RL3")
    orch = Orchestrator("max", Path("/tmp"), "TEST-RL3", "brief")
    orch.run_id = run_id

    mock_client = MagicMock()
    mock_client.receive_response = MagicMock(side_effect=[
        _async_gen_raising(MessageParseError("Unknown message type: something_weird", {})),
    ])

    with pytest.raises(MessageParseError):
        await orch._main_loop(mock_client)


# ─── Normal _main_loop flow ──────────────────────────────────────


@pytest.mark.asyncio
async def test_main_loop_completes_without_checkpoint():
    """_main_loop exits cleanly when no checkpoint is raised."""
    run_id = _make_run(ticket="TEST-NC1")
    orch = Orchestrator("max", Path("/tmp"), "TEST-NC1", "brief")
    orch.run_id = run_id

    mock_client = MagicMock()
    mock_client.receive_response = MagicMock(return_value=_async_gen(_result_msg()))

    await orch._main_loop(mock_client)

    assert not orch._awaiting_approval
    assert not orch.stop_requested
    assert mock_client.receive_response.call_count == 1


@pytest.mark.asyncio
async def test_main_loop_stop_requested_exits_early():
    """If stop_requested is set during iteration, _main_loop returns immediately."""
    run_id = _make_run(ticket="TEST-STOP1")
    orch = Orchestrator("max", Path("/tmp"), "TEST-STOP1", "brief")
    orch.run_id = run_id
    orch.stop_requested = True

    mock_client = MagicMock()
    mock_client.receive_response = MagicMock(return_value=_async_gen(_result_msg()))

    await orch._main_loop(mock_client)

    mock_client.receive_response.assert_not_called()


# ─── Approval checkpoint flow ────────────────────────────────────


@pytest.mark.asyncio
async def test_main_loop_drains_response_and_exits():
    """_main_loop just drains receive_response() — approval is handled in the hook."""
    run_id = _make_run(ticket="TEST-DRAIN")
    orch = Orchestrator("max", Path("/tmp"), "TEST-DRAIN", "brief")
    orch.run_id = run_id

    mock_client = MagicMock()
    mock_client.receive_response = MagicMock(return_value=_async_gen(_result_msg()))

    await orch._main_loop(mock_client)

    assert mock_client.receive_response.call_count == 1


# ─── _capture_session_id ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_session_id_from_system_message():
    """_capture_session_id writes SDK session ID to DB on first SystemMessage."""
    run_id = _make_run(ticket="TEST-SID")
    orch = Orchestrator("max", Path("/tmp"), "TEST-SID", "brief")
    orch.run_id = run_id

    sys_msg = SystemMessage(subtype="init", data={"session_id": "sdk-session-abc"})
    orch._capture_session_id(sys_msg)

    assert orch.sdk_session_id == "sdk-session-abc"
    with db_session() as db:
        run = db.query(Run).filter_by(id=run_id).one()
        assert run.sdk_session_id == "sdk-session-abc"


@pytest.mark.asyncio
async def test_capture_session_id_only_once():
    """_capture_session_id does not overwrite an already-captured session ID."""
    run_id = _make_run(ticket="TEST-SID2")
    orch = Orchestrator("max", Path("/tmp"), "TEST-SID2", "brief")
    orch.run_id = run_id
    orch.sdk_session_id = "already-set"

    sys_msg = SystemMessage(subtype="init", data={"session_id": "new-id"})
    orch._capture_session_id(sys_msg)

    assert orch.sdk_session_id == "already-set"


# ─── _record_decision ────────────────────────────────────────────


def test_record_decision_updates_checkpoint_and_restores_state():
    init_db()
    with db_session() as db:
        run = Run(
            agent="max", ticket="TEST-RD1", cwd="/tmp", initial_prompt="test",
            state="needs_approval",
        )
        run.state_before_pause = "planning"
        db.add(run)
        db.flush()
        run_id = run.id
        cp = Checkpoint(run_id=run_id, kind="plan_ready", summary="Plan summary")
        db.add(cp)
        db.flush()

    orch = Orchestrator("max", Path("/tmp"), "TEST-RD1", "brief")
    orch.run_id = run_id

    orch._record_decision("approved", "looks great")

    with db_session() as db:
        cp = db.query(Checkpoint).filter_by(run_id=run_id).one()
        assert cp.decision == "approved"
        assert cp.decision_note == "looks great"
        assert cp.decided_at is not None
        run = db.query(Run).filter_by(id=run_id).one()
        assert run.state == "planning"  # restored from state_before_pause


def test_record_decision_empty_note_stored_as_none():
    init_db()
    with db_session() as db:
        run = Run(
            agent="max", ticket="TEST-RD2", cwd="/tmp", initial_prompt="test",
            state="needs_approval",
        )
        run.state_before_pause = "in_development"
        db.add(run)
        db.flush()
        run_id = run.id
        cp = Checkpoint(run_id=run_id, kind="pre_push", summary="Ready to push")
        db.add(cp)
        db.flush()

    orch = Orchestrator("max", Path("/tmp"), "TEST-RD2", "brief")
    orch.run_id = run_id

    orch._record_decision("approved", "")

    with db_session() as db:
        cp = db.query(Checkpoint).filter_by(run_id=run_id).one()
        assert cp.decision_note is None  # empty string → None


# ─── make_checkpoint_hook ────────────────────────────────────────


@pytest.mark.asyncio
async def test_checkpoint_hook_ignores_non_checkpoint_tools():
    from ranch.runner.checkpoints import make_checkpoint_hook

    orch = MagicMock()
    orch.on_checkpoint = AsyncMock()
    hook_fn = make_checkpoint_hook(orch).hooks[0]

    result = await hook_fn({"tool_name": "Bash", "tool_input": {}}, "tid", MagicMock())

    orch.on_checkpoint.assert_not_called()
    assert result == {}


@pytest.mark.asyncio
async def test_checkpoint_hook_tests_green_no_pause():
    from ranch.runner.checkpoints import make_checkpoint_hook, CHECKPOINT_TOOL

    orch = MagicMock()
    orch.on_checkpoint = AsyncMock()
    hook_fn = make_checkpoint_hook(orch).hooks[0]

    result = await hook_fn(
        {"tool_name": CHECKPOINT_TOOL, "tool_input": {"kind": "tests_green", "summary": "All pass"}},
        "tid",
        MagicMock(),
    )

    orch.on_checkpoint.assert_called_once_with("tests_green", "All pass", None)
    assert result == {}  # no hookSpecificOutput — doesn't require approval


@pytest.mark.asyncio
async def test_checkpoint_hook_plan_ready_returns_typed_decision():
    """Hook awaits the approval and returns the typed HumanDecision as additionalContext."""
    from ranch.runner.checkpoints import make_checkpoint_hook, CHECKPOINT_TOOL

    orch = MagicMock()
    orch.on_checkpoint = AsyncMock()
    orch._approval_ready = asyncio.Event()
    orch._approval_ready.set()  # pre-set so the hook doesn't actually block
    orch._approval_result = "approved"
    orch._awaiting_approval = True
    orch._record_decision = MagicMock()
    orch.ticket = "ECD-1234"
    hook_fn = make_checkpoint_hook(orch).hooks[0]

    result = await hook_fn(
        {"tool_name": CHECKPOINT_TOOL, "tool_input": {"kind": "plan_ready", "summary": "Plan done", "payload": {"files": ["a.py"]}}},
        "tid",
        MagicMock(),
    )

    orch.on_checkpoint.assert_called_once_with("plan_ready", "Plan done", {"files": ["a.py"]})
    orch._record_decision.assert_called_once_with("approved", "")
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "HUMAN DECISION" in ctx
    assert "plan_ready" in ctx
    assert "APPROVED" in ctx


@pytest.mark.asyncio
async def test_checkpoint_hook_pre_push_returns_typed_decision_with_branch():
    from ranch.runner.checkpoints import make_checkpoint_hook, CHECKPOINT_TOOL

    orch = MagicMock()
    orch.on_checkpoint = AsyncMock()
    orch._approval_ready = asyncio.Event()
    orch._approval_ready.set()
    orch._approval_result = "approved"
    orch._awaiting_approval = True
    orch._record_decision = MagicMock()
    orch.ticket = "ECD-1589"
    hook_fn = make_checkpoint_hook(orch).hooks[0]

    result = await hook_fn(
        {"tool_name": CHECKPOINT_TOOL, "tool_input": {"kind": "pre_push", "summary": "Ready"}},
        "tid",
        MagicMock(),
    )

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "HUMAN DECISION" in ctx
    assert "pre_push" in ctx
    assert "APPROVED" in ctx
    assert "ecd-1589" in ctx.lower()  # branch hint
    assert "push to origin" in ctx.lower()


@pytest.mark.asyncio
async def test_checkpoint_hook_rejection_returns_typed_rejection():
    from ranch.runner.checkpoints import make_checkpoint_hook, CHECKPOINT_TOOL

    orch = MagicMock()
    orch.on_checkpoint = AsyncMock()
    orch._approval_ready = asyncio.Event()
    orch._approval_ready.set()
    orch._approval_result = "rejected — scope too wide"
    orch._awaiting_approval = True
    orch._record_decision = MagicMock()
    orch.ticket = "ECD-1"
    hook_fn = make_checkpoint_hook(orch).hooks[0]

    result = await hook_fn(
        {"tool_name": CHECKPOINT_TOOL, "tool_input": {"kind": "plan_ready", "summary": "Plan"}},
        "tid",
        MagicMock(),
    )

    orch._record_decision.assert_called_once_with("rejected", "scope too wide")
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "REJECTED" in ctx
    assert "scope too wide" in ctx


# ─── initial_user_prompt ─────────────────────────────────────────


def test_initial_user_prompt_standard_includes_plan_step():
    from ranch.runner.prompts import initial_user_prompt

    prompt = initial_user_prompt("ECD-123", "fix the export bug")
    assert "ECD-123" in prompt
    assert "fix the export bug" in prompt
    assert "Begin with the PLAN step." in prompt


def test_initial_user_prompt_free_excludes_plan_step():
    from ranch.runner.prompts import initial_user_prompt

    prompt = initial_user_prompt("ECD-123", "review this PR", free=True)
    assert "ECD-123" in prompt
    assert "review this PR" in prompt
    assert "Begin with the PLAN step." not in prompt


def test_initial_user_prompt_ticket_and_brief_both_present():
    from ranch.runner.prompts import initial_user_prompt

    ticket = "ECD-999"
    brief = "Some detailed brief text here."
    prompt = initial_user_prompt(ticket, brief)
    assert ticket in prompt
    assert brief in prompt


# ─── auto_approve mode ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_checkpoint_auto_approve_fires_immediately():
    """In auto_approve mode, an approval-required checkpoint sets the event itself."""
    run_id = _make_run(ticket="TEST-AUTO1")
    orch = Orchestrator("max", Path("/tmp"), "TEST-AUTO1", "brief", auto_approve=True)
    orch.run_id = run_id

    await orch.on_checkpoint("plan_ready", "Plan summary", None)

    assert orch._awaiting_approval is True
    assert orch._approval_ready.is_set()
    assert orch._approval_result == "approved"


@pytest.mark.asyncio
async def test_on_checkpoint_no_auto_approve_waits():
    """Without auto_approve, the event stays unset until a human acts."""
    run_id = _make_run(ticket="TEST-AUTO2")
    orch = Orchestrator("max", Path("/tmp"), "TEST-AUTO2", "brief", auto_approve=False)
    orch.run_id = run_id

    await orch.on_checkpoint("pre_push", "Push summary", None)

    assert orch._awaiting_approval is True
    assert not orch._approval_ready.is_set()
    assert orch._approval_result is None


@pytest.mark.asyncio
async def test_on_checkpoint_tests_green_does_not_auto_approve():
    """tests_green isn't an approval checkpoint — auto_approve shouldn't touch it."""
    run_id = _make_run(ticket="TEST-AUTO3")
    orch = Orchestrator("max", Path("/tmp"), "TEST-AUTO3", "brief", auto_approve=True)
    orch.run_id = run_id

    await orch.on_checkpoint("tests_green", "All pass", None)

    assert orch._awaiting_approval is False
    assert not orch._approval_ready.is_set()


@pytest.mark.asyncio
async def test_auto_approve_hook_returns_typed_decision_immediately():
    """End-to-end: in auto_approve mode, the hook produces the typed approval without
    needing any external trigger."""
    from ranch.runner.checkpoints import make_checkpoint_hook, CHECKPOINT_TOOL

    run_id = _make_run(ticket="TEST-AUTO4")
    orch = Orchestrator("max", Path("/tmp"), "TEST-AUTO4", "brief", auto_approve=True)
    orch.run_id = run_id
    hook_fn = make_checkpoint_hook(orch).hooks[0]

    result = await hook_fn(
        {"tool_name": CHECKPOINT_TOOL, "tool_input": {"kind": "pre_push", "summary": "ready"}},
        "tid",
        MagicMock(),
    )

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "HUMAN DECISION" in ctx
    assert "APPROVED" in ctx
    assert "pre_push" in ctx
