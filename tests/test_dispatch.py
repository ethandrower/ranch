"""Tests for `ranch dispatch` (Plan B / Issue #1).

Full subprocess spawning is covered by the E2E smoke at the end. Here we test:
- Orchestrator.run() reuses a pre-created Run row when self.run_id is set
- Orchestrator.run() creates a new Run row when self.run_id is None
- Stdin loop is skipped when stdin isn't a TTY (simulates detached mode)
- `ranch dispatch` CLI validates the agent exists before forking
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from ranch.cli import cli
from ranch.db import db_session, init_db
from ranch.models import Run
from ranch.runner.orchestrator import Orchestrator


# ─── Orchestrator Run row creation / reuse ────────────────────────


@pytest.mark.asyncio
async def test_run_creates_row_when_run_id_none():
    """Foreground path: Orchestrator.run() with no pre-set run_id creates a Run row."""
    init_db()
    orch = Orchestrator("max", Path("/tmp"), "TEST-CREATE", "brief")
    assert orch.run_id is None

    # Stub out the SDK session — we only care about the pre-loop setup.
    with patch("ranch.runner.orchestrator.ClaudeSDKClient") as mock_cls:
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.query = AsyncMock()
        mock_client.receive_response = MagicMock(return_value=_empty_stream())
        mock_cls.return_value = mock_client
        await orch.run()

    assert orch.run_id is not None
    with db_session() as db:
        row = db.query(Run).filter_by(id=orch.run_id).one()
        assert row.ticket == "TEST-CREATE"
        assert row.agent == "max"


@pytest.mark.asyncio
async def test_run_reuses_row_when_run_id_preset():
    """Dispatched path: Orchestrator.run() with pre-set run_id reuses the row,
    does NOT create a duplicate, and transitions its state to 'planning'."""
    init_db()
    with db_session() as db:
        row = Run(
            agent="max",
            ticket="TEST-REUSE",
            cwd="/tmp",
            initial_prompt="brief",
            state="queued",
            dispatch_mode="background",
        )
        db.add(row)
        db.flush()
        preset_id = row.id

    orch = Orchestrator("max", Path("/tmp"), "TEST-REUSE", "brief")
    orch.run_id = preset_id

    with patch("ranch.runner.orchestrator.ClaudeSDKClient") as mock_cls:
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.query = AsyncMock()
        mock_client.receive_response = MagicMock(return_value=_empty_stream())
        mock_cls.return_value = mock_client
        await orch.run()

    assert orch.run_id == preset_id
    with db_session() as db:
        rows = db.query(Run).filter_by(ticket="TEST-REUSE").all()
        assert len(rows) == 1  # no duplicate created
        assert rows[0].state == "completed"  # ran through and finalized


# ─── Stdin loop gating ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stdin_loop_skipped_when_stdin_not_tty():
    """When stdin isn't a TTY (detached mode), the stdin task isn't started.
    The poll loop still runs so out-of-process interjections work."""
    init_db()
    orch = Orchestrator("max", Path("/tmp"), "TEST-NO-TTY", "brief")

    created_tasks = []
    original_create_task = asyncio.create_task

    def spy_create_task(coro, *args, **kwargs):
        task = original_create_task(coro, *args, **kwargs)
        name = getattr(coro, "__name__", str(coro))
        # Drill into coroutine frame if possible
        frame = getattr(coro, "cr_code", None)
        if frame is not None:
            name = frame.co_name
        created_tasks.append(name)
        return task

    with patch("ranch.runner.orchestrator.ClaudeSDKClient") as mock_cls, \
         patch.object(sys.stdin, "isatty", return_value=False), \
         patch("ranch.runner.orchestrator.asyncio.create_task", side_effect=spy_create_task):
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.query = AsyncMock()
        mock_client.receive_response = MagicMock(return_value=_empty_stream())
        mock_cls.return_value = mock_client
        await orch.run()

    assert "_db_poll_loop" in created_tasks
    assert "_stdin_loop" not in created_tasks


@pytest.mark.asyncio
async def test_stdin_loop_runs_when_stdin_is_tty():
    init_db()
    orch = Orchestrator("max", Path("/tmp"), "TEST-TTY", "brief")

    created_tasks = []
    original_create_task = asyncio.create_task

    def spy_create_task(coro, *args, **kwargs):
        task = original_create_task(coro, *args, **kwargs)
        frame = getattr(coro, "cr_code", None)
        if frame is not None:
            created_tasks.append(frame.co_name)
        return task

    with patch("ranch.runner.orchestrator.ClaudeSDKClient") as mock_cls, \
         patch.object(sys.stdin, "isatty", return_value=True), \
         patch("ranch.runner.orchestrator.asyncio.create_task", side_effect=spy_create_task):
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.query = AsyncMock()
        mock_client.receive_response = MagicMock(return_value=_empty_stream())
        mock_cls.return_value = mock_client
        await orch.run()

    assert "_db_poll_loop" in created_tasks
    assert "_stdin_loop" in created_tasks


# ─── dispatch CLI surface ─────────────────────────────────────────


def test_dispatch_rejects_unknown_agent(tmp_path, monkeypatch):
    """dispatch aborts with non-zero exit when the agent isn't in config."""
    init_db()
    monkeypatch.setattr("ranch.config.reload_agents", lambda: {})
    monkeypatch.setattr("ranch.cli.reload_agents", lambda: {})

    result = CliRunner().invoke(cli, [
        "dispatch", "nonexistent",
        "--ticket", "TEST-X",
        "--brief", "do a thing",
    ])
    assert result.exit_code != 0
    assert "unknown agent" in result.output.lower()


def test_dispatch_creates_queued_row_then_spawns(tmp_path, monkeypatch):
    """dispatch writes a state='queued' row, records PID+log_path, and spawns
    the detached subprocess exactly once."""
    init_db()

    fake_worktree = tmp_path / "work"
    fake_worktree.mkdir()

    from ranch.config import Agent
    fake_agents = {"max": Agent(name="max", worktree=fake_worktree)}
    monkeypatch.setattr("ranch.cli.reload_agents", lambda: fake_agents)

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr("ranch.config.LOG_DIR", log_dir)
    # The cli module imports LOG_DIR inside the handler, so patch it there too
    import ranch.config as _cfg
    monkeypatch.setattr(_cfg, "LOG_DIR", log_dir)

    fake_proc = MagicMock()
    fake_proc.pid = 424242

    with patch("subprocess.Popen", return_value=fake_proc) as mock_popen:
        result = CliRunner().invoke(cli, [
            "dispatch", "max",
            "--ticket", "TEST-DISP",
            "--brief", "do the thing",
        ])

    assert result.exit_code == 0, result.output
    assert "Dispatched run" in result.output
    assert "424242" in result.output

    # Exactly one subprocess spawn, with detach flags
    import subprocess as _sp
    mock_popen.assert_called_once()
    _, kwargs = mock_popen.call_args
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == _sp.DEVNULL

    with db_session() as db:
        rows = db.query(Run).filter_by(ticket="TEST-DISP").all()
        assert len(rows) == 1
        r = rows[0]
        assert r.state == "queued"
        assert r.dispatch_mode == "background"
        assert r.pid == 424242
        assert r.log_path and r.log_path.endswith(f"run_{r.id}.log")


# ─── helpers ──────────────────────────────────────────────────────


async def _empty_stream():
    """Simulate an SDK stream that completes immediately."""
    return
    yield  # makes it an async generator
