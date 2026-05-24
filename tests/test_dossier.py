"""Tests for the dossier hook + persistence (Phase H2a).

Covers:
- make_dossier_hook ignores non-record_state tools
- make_dossier_hook validates input via RecordStateInput
- make_dossier_hook is non-blocking (no approval gate)
- Orchestrator.on_state writes a Dossier row
- Multiple calls accumulate (history preserved); latest-wins query works
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ranch.db import db_session, init_db
from ranch.models import Dossier, Run
from ranch.runner.dossier import STATE_TOOL, make_dossier_hook
from ranch.runner.messages import RecordStateInput
from ranch.runner.orchestrator import Orchestrator


def _valid_input(**overrides) -> dict:
    base = {
        "plan": [{"step": "Read the ticket", "status": "done"}],
        "just_did": "Finished reading the ticket and the linked epic.",
        "state": "planning",
    }
    base.update(overrides)
    return base


def _make_run(ticket: str = "TEST-DOSS") -> int:
    init_db()
    with db_session() as db:
        run = Run(agent="max", ticket=ticket, cwd="/tmp", initial_prompt="brief", state="planning")
        db.add(run)
        db.flush()
        return run.id


# ─── make_dossier_hook ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dossier_hook_ignores_non_state_tools():
    orch = MagicMock()
    orch.on_state = AsyncMock()
    hook_fn = make_dossier_hook(orch).hooks[0]

    result = await hook_fn({"tool_name": "Bash", "tool_input": {}}, "tid", MagicMock())

    orch.on_state.assert_not_called()
    assert result == {}


@pytest.mark.asyncio
async def test_dossier_hook_calls_on_state_with_validated_payload():
    orch = MagicMock()
    orch.on_state = AsyncMock()
    hook_fn = make_dossier_hook(orch).hooks[0]

    result = await hook_fn(
        {"tool_name": STATE_TOOL, "tool_input": _valid_input()},
        "tid",
        MagicMock(),
    )

    orch.on_state.assert_called_once()
    passed = orch.on_state.call_args[0][0]
    assert isinstance(passed, RecordStateInput)
    assert passed.state == "planning"
    assert result == {}  # non-blocking — no approval gate


@pytest.mark.asyncio
async def test_dossier_hook_validation_error_returns_context():
    orch = MagicMock()
    orch.on_state = AsyncMock()
    hook_fn = make_dossier_hook(orch).hooks[0]

    result = await hook_fn(
        {"tool_name": STATE_TOOL, "tool_input": {"plan": [], "state": "bogus", "just_did": "x"}},
        "tid",
        MagicMock(),
    )

    orch.on_state.assert_not_called()
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "record_state validation error" in ctx


# ─── Orchestrator.on_state persistence ────────────────────────────


@pytest.mark.asyncio
async def test_on_state_persists_dossier_row():
    run_id = _make_run()
    orch = Orchestrator("max", Path("/tmp"), "TEST-DOSS", "brief")
    orch.run_id = run_id

    dossier = RecordStateInput.model_validate(_valid_input(state="coding"))
    await orch.on_state(dossier)

    with db_session() as db:
        rows = db.query(Dossier).filter_by(run_id=run_id).all()
        assert len(rows) == 1
        assert rows[0].state == "coding"
        payload = json.loads(rows[0].payload_json)
        assert payload["just_did"] == "Finished reading the ticket and the linked epic."


@pytest.mark.asyncio
async def test_on_state_accumulates_history_and_latest_wins():
    run_id = _make_run()
    orch = Orchestrator("max", Path("/tmp"), "TEST-DOSS-2", "brief")
    orch.run_id = run_id

    # Three sequential updates simulating phase transitions.
    await orch.on_state(RecordStateInput.model_validate(_valid_input(state="planning", just_did="Drew up the plan.")))
    await asyncio.sleep(0.01)  # ensure created_at ordering is monotonic on SQLite
    await orch.on_state(RecordStateInput.model_validate(_valid_input(state="coding", just_did="Wrote the failing test.")))
    await asyncio.sleep(0.01)
    await orch.on_state(RecordStateInput.model_validate(_valid_input(
        state="parked",
        just_did="Tests green, ready for review.",
        blocker="Waiting on pre_push approval.",
        options=[{"label": "approve", "description": "Proceed."}],
    )))

    with db_session() as db:
        all_rows = db.query(Dossier).filter_by(run_id=run_id).order_by(Dossier.created_at.asc()).all()
        assert len(all_rows) == 3
        # History preserved in chronological order
        assert [r.state for r in all_rows] == ["planning", "coding", "parked"]

        latest = (
            db.query(Dossier)
            .filter_by(run_id=run_id)
            .order_by(Dossier.created_at.desc())
            .first()
        )
        assert latest.state == "parked"
        payload = json.loads(latest.payload_json)
        assert payload["blocker"] == "Waiting on pre_push approval."
        assert payload["options"][0]["label"] == "approve"
