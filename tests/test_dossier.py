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


# ─── CLI helpers (Phase H2c) ──────────────────────────────────────


def _persist_dossier(run_id: int, **overrides):
    """Test helper — write a Dossier row directly."""
    from ranch.runner.messages import RecordStateInput
    data = {
        "plan": [{"step": "Plan it", "status": "done"}],
        "just_did": "Wrote the plan.",
        "state": "planning",
    }
    data.update(overrides)
    payload = RecordStateInput.model_validate(data)
    with db_session() as db:
        row = Dossier(run_id=run_id, state=payload.state, payload_json=payload.model_dump_json())
        db.add(row)


def test_fetch_latest_dossier_returns_none_for_missing_run():
    from ranch.cli import _fetch_latest_dossier
    init_db()
    run, payload = _fetch_latest_dossier(999_999)
    assert run is None
    assert payload is None


def test_fetch_latest_dossier_returns_none_payload_when_no_dossier():
    from ranch.cli import _fetch_latest_dossier
    rid = _make_run(ticket="TEST-NODOSS")
    run, payload = _fetch_latest_dossier(rid)
    assert run is not None
    assert run["agent"] == "max"
    assert payload is None


def test_fetch_latest_dossier_returns_most_recent():
    from ranch.cli import _fetch_latest_dossier
    rid = _make_run(ticket="TEST-LATEST")
    _persist_dossier(rid, state="planning", just_did="First.")
    # SQLite created_at resolution: nudge before the second insert
    import time as _time
    _time.sleep(0.01)
    _persist_dossier(rid, state="parked", just_did="Final.", blocker="Need approval.")
    run, payload = _fetch_latest_dossier(rid)
    assert payload["state"] == "parked"
    assert payload["blocker"] == "Need approval."
    assert "_updated_at" in payload  # surfaced for "last updated X ago" UI later


def test_render_dossier_panel_handles_missing_payload():
    """Render shouldn't blow up when there's no dossier yet."""
    from ranch.cli import _render_dossier_panel
    panel = _render_dossier_panel({"id": 1, "agent": "max", "ticket": None, "state": "queued"}, None)
    # Just ensure it constructs; rich will render it on console.print
    assert panel is not None


def test_render_dossier_panel_shows_details_preview():
    """When `details` is populated, the panel shows a short preview."""
    from ranch.cli import _render_dossier_panel
    payload = {
        "state": "coding",
        "just_did": "Wrote the failing test.",
        "details": (
            "Started by reading existing tests in tests/test_foo.py.\n"
            "Identified the pattern: each test uses CliRunner + isolated DB.\n"
            "Wrote a matching test for the new endpoint. Ran it — fails as expected."
        ),
        "plan": [{"step": "Test it", "status": "in_progress"}],
    }
    panel = _render_dossier_panel(
        {"id": 1, "agent": "max", "ticket": "ECD-1", "state": "in_development"},
        payload,
    )
    from io import StringIO
    from rich.console import Console as RichConsole
    buf = StringIO()
    RichConsole(file=buf, force_terminal=False, width=120).print(panel)
    output = buf.getvalue()
    assert "Details:" in output
    assert "Started by reading existing tests" in output


def test_render_dossier_panel_skips_details_section_when_absent():
    """Routine emissions without `details` shouldn't render the section."""
    from ranch.cli import _render_dossier_panel
    payload = {"state": "coding", "just_did": "Bumped a version.",
               "plan": [{"step": "Bump", "status": "done"}]}
    panel = _render_dossier_panel(
        {"id": 1, "agent": "max", "ticket": "ECD-1", "state": "in_development"},
        payload,
    )
    from io import StringIO
    from rich.console import Console as RichConsole
    buf = StringIO()
    RichConsole(file=buf, force_terminal=False, width=120).print(panel)
    assert "Details:" not in buf.getvalue()


def test_render_dossier_panel_includes_all_sections():
    from ranch.cli import _render_dossier_panel
    payload = {
        "state": "parked",
        "just_did": "Tests green.",
        "blocker": "Need pre_push approval.",
        "plan": [
            {"step": "Plan it", "status": "done"},
            {"step": "Build it", "status": "in_progress", "notes": "halfway through"},
        ],
        "options": [{"label": "approve", "description": "Proceed."}],
        "files_touched": ["a.py", "b.py"],
    }
    panel = _render_dossier_panel(
        {"id": 42, "agent": "jeffy", "ticket": "ECD-1", "state": "needs_approval"},
        payload,
    )
    # Walk the renderable's plain text to check content survived rendering setup.
    from io import StringIO
    from rich.console import Console
    buf = StringIO()
    Console(file=buf, force_terminal=False, width=120).print(panel)
    output = buf.getvalue()
    assert "Run #42" in output
    assert "jeffy / ECD-1" in output
    assert "parked" in output
    assert "Tests green." in output
    assert "Need pre_push approval." in output
    assert "Plan it" in output
    assert "Build it" in output
    assert "halfway through" in output
    assert "approve" in output
    assert "Files touched" in output


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
