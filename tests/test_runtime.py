"""Tests for Plan C — runtime helpers + status/watch/log CLI surface."""
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from ranch.cli import cli
from ranch.db import db_session, init_db
from ranch.models import Checkpoint, Interjection, Run
from ranch.runtime import (
    is_alive,
    mark_orphans,
    snapshot_states,
    watch_for_change,
)


# ─── is_alive ─────────────────────────────────────────────────────


def test_is_alive_for_running_self():
    import os
    assert is_alive(os.getpid()) is True


def test_is_alive_for_obviously_dead_pid():
    # PID 1 always exists (init). A very large PID that won't exist.
    assert is_alive(999_999_999) is False


def test_is_alive_handles_none_and_zero():
    assert is_alive(None) is False
    assert is_alive(0) is False
    assert is_alive(-1) is False


# ─── mark_orphans ─────────────────────────────────────────────────


def _make_run(**kwargs):
    defaults = dict(
        agent="max", ticket="TEST", cwd="/tmp", initial_prompt="b",
        state="in_development", dispatch_mode="background",
    )
    defaults.update(kwargs)
    init_db()
    with db_session() as db:
        run = Run(**defaults)
        db.add(run)
        db.flush()
        return run.id


def test_mark_orphans_reaps_dead_pid():
    run_id = _make_run(pid=999_999_999, state="in_development")
    reaped = mark_orphans()
    assert run_id in reaped
    with db_session() as db:
        r = db.query(Run).filter_by(id=run_id).one()
        assert r.state == "error"
        assert r.exit_reason == "orphaned"
        assert r.ended_at is not None


def test_mark_orphans_skips_alive_pid():
    import os
    run_id = _make_run(pid=os.getpid(), state="in_development")
    reaped = mark_orphans()
    assert run_id not in reaped
    with db_session() as db:
        assert db.query(Run).filter_by(id=run_id).one().state == "in_development"


def test_mark_orphans_skips_terminal_states():
    run_id = _make_run(pid=999_999_999, state="completed")
    reaped = mark_orphans()
    assert run_id not in reaped


def test_mark_orphans_skips_foreground_runs():
    run_id = _make_run(pid=999_999_999, state="in_development", dispatch_mode="foreground")
    reaped = mark_orphans()
    assert run_id not in reaped


def test_mark_orphans_is_idempotent():
    run_id = _make_run(pid=999_999_999, state="in_development")
    assert run_id in mark_orphans()
    # Second call: already in terminal state → no-op
    assert mark_orphans() == []


# ─── snapshot_states ──────────────────────────────────────────────


def test_snapshot_states_filters_terminal_by_default():
    rid1 = _make_run(state="in_development", ticket="A")
    rid2 = _make_run(state="completed", ticket="B")
    snap = snapshot_states()
    assert rid1 in snap
    assert rid2 not in snap


def test_snapshot_states_with_explicit_ids():
    rid1 = _make_run(state="completed", ticket="A")
    snap = snapshot_states([rid1])
    assert snap[rid1] == "completed"


# ─── watch_for_change ─────────────────────────────────────────────


def test_watch_for_change_detects_transition():
    """watch returns once a state change occurs on the first poll tick."""
    rid = _make_run(state="in_development", ticket="WATCH-1")

    # Fake clock/sleep so we don't actually block
    ticks = {"n": 0}
    def fake_sleep(s):
        # On first sleep, mutate the DB to simulate the orchestrator transitioning
        if ticks["n"] == 0:
            with db_session() as db:
                db.query(Run).filter_by(id=rid).update({"state": "needs_approval"})
        ticks["n"] += 1

    result = watch_for_change(
        run_ids=[rid],
        timeout_seconds=10.0,
        poll_interval=0.01,
        clock=lambda: 0.0,  # never hit deadline
        sleep=fake_sleep,
    )
    assert result == (rid, "needs_approval")


def test_watch_for_change_respects_timeout():
    """watch returns None if no transition happens before the deadline."""
    rid = _make_run(state="in_development", ticket="WATCH-2")
    now = {"t": 0.0}
    def fake_clock():
        return now["t"]
    def fake_sleep(s):
        now["t"] += s  # advance simulated time

    result = watch_for_change(
        run_ids=[rid],
        timeout_seconds=0.5,
        poll_interval=0.1,
        clock=fake_clock,
        sleep=fake_sleep,
    )
    assert result is None


def test_watch_for_change_detects_terminal_transition():
    """A non-terminal → terminal transition is reported with the final state."""
    rid = _make_run(state="in_development", ticket="WATCH-3")
    ticks = {"n": 0}
    def fake_sleep(s):
        if ticks["n"] == 0:
            with db_session() as db:
                db.query(Run).filter_by(id=rid).update({"state": "completed"})
        ticks["n"] += 1

    # Default filter (run_ids=None) excludes terminals, so the run "disappears"
    # from the snapshot. watch should still report the real final state.
    result = watch_for_change(
        run_ids=None,
        timeout_seconds=5.0,
        poll_interval=0.01,
        clock=lambda: 0.0,
        sleep=fake_sleep,
    )
    assert result is not None
    assert result[0] == rid
    assert result[1] == "completed"


# ─── status CLI ───────────────────────────────────────────────────


def test_status_no_arg_renders_active_runs(monkeypatch):
    init_db()
    # Avoid pulling real agents from ~/.ranch/config.toml during test
    monkeypatch.setattr("ranch.cli.reload_agents", lambda: {})

    with db_session() as db:
        db.add(Run(agent="max", ticket="ALIVE-1", cwd="/tmp", initial_prompt="x",
                   state="in_development"))
        db.add(Run(agent="max", ticket="DONE-1", cwd="/tmp", initial_prompt="x",
                   state="completed"))

    result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "ALIVE-1" in result.output
    assert "DONE-1" not in result.output  # terminal → excluded


def test_status_no_arg_empty(monkeypatch):
    init_db()
    monkeypatch.setattr("ranch.cli.reload_agents", lambda: {})
    result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "No active runs" in result.output


def test_status_with_run_id_shows_detail():
    init_db()
    with db_session() as db:
        run = Run(agent="max", ticket="DETAIL-1", cwd="/tmp", initial_prompt="x",
                  state="needs_approval")
        db.add(run)
        db.flush()
        run_id = run.id
        db.add(Checkpoint(run_id=run_id, kind="plan_ready", summary="The plan..."))

    result = CliRunner().invoke(cli, ["status", str(run_id)])
    assert result.exit_code == 0
    assert "DETAIL-1" in result.output
    assert "needs_approval" in result.output
    assert "plan_ready" in result.output
    assert "The plan" in result.output
    assert f"ranch approve {run_id}" in result.output


def test_status_unknown_run_aborts():
    init_db()
    result = CliRunner().invoke(cli, ["status", "99999"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_status_marks_orphans_on_view(monkeypatch):
    """Running status triggers orphan reaping as a side effect."""
    init_db()
    monkeypatch.setattr("ranch.cli.reload_agents", lambda: {})

    with db_session() as db:
        run = Run(agent="max", ticket="ORPH-1", cwd="/tmp", initial_prompt="x",
                  state="in_development", dispatch_mode="background", pid=999_999_999)
        db.add(run)
        db.flush()
        rid = run.id

    result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "Reaped" in result.output

    with db_session() as db:
        assert db.query(Run).filter_by(id=rid).one().state == "error"


# ─── watch CLI ────────────────────────────────────────────────────


def test_watch_cli_silent_on_timeout(monkeypatch):
    """watch with --timeout exits silently when no state change occurs."""
    init_db()
    _make_run(state="in_development", ticket="W-SILENT")

    # Short timeout so the test runs fast
    result = CliRunner().invoke(cli, ["watch", "--timeout", "0.3"])
    assert result.exit_code == 0
    # No "Run #" line printed
    assert "Run #" not in result.output


def test_watch_cli_prints_on_transition():
    """watch prints `Run #<id> → <state>` when a transition is detected.

    We stub watch_for_change at its source module — the cli's function-local
    import resolves through the module attribute, so patching ranch.runtime
    catches it.
    """
    init_db()
    rid = _make_run(state="in_development", ticket="W-CHANGE")

    with patch("ranch.runtime.watch_for_change", return_value=(rid, "needs_approval")):
        result = CliRunner().invoke(cli, ["watch"])

    assert result.exit_code == 0
    assert f"Run #{rid}" in result.output
    assert "needs_approval" in result.output


# ─── log CLI ──────────────────────────────────────────────────────


def test_log_cli_prints_log_path():
    init_db()
    with db_session() as db:
        run = Run(
            agent="max", ticket="LOG-1", cwd="/tmp", initial_prompt="x",
            state="in_development", log_path="/tmp/ranch/run_1.log",
        )
        db.add(run)
        db.flush()
        run_id = run.id

    result = CliRunner().invoke(cli, ["log", str(run_id)])
    assert result.exit_code == 0
    assert "/tmp/ranch/run_1.log" in result.output


def test_log_cli_aborts_on_foreground_run():
    init_db()
    with db_session() as db:
        run = Run(agent="max", ticket="LOG-2", cwd="/tmp", initial_prompt="x",
                  state="in_development")  # no log_path
        db.add(run)
        db.flush()
        run_id = run.id

    result = CliRunner().invoke(cli, ["log", str(run_id)])
    assert result.exit_code != 0
    assert "no log file" in result.output.lower()


def test_log_cli_unknown_run():
    init_db()
    result = CliRunner().invoke(cli, ["log", "99999"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()
