"""Tests for the ranch hand scheduler (Phase H11).

The Jira side, the scope side, and the propose side are all injected as
fakes — these tests exercise the decision logic. End-to-end real-agent
behavior lives in the tier-2 validation script.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from ranch.db import db_session, init_db
from ranch.hand import (
    AWAITING_APPROVAL_WINDOW,
    HANDS_DIR,
    RanchHand,
    _active_run_for,
    _last_parked_run_for,
    _pid_file,
    _read_pid,
    _stop_file,
    get_hand_status,
    list_all_hand_statuses,
    request_stop,
)
from ranch.models import Dossier, Run


# ─── Fixture: isolate the hands dir per test ──────────────────────


@pytest.fixture(autouse=True)
def isolated_hands_dir(tmp_path, monkeypatch):
    h = tmp_path / "hands"
    h.mkdir()
    monkeypatch.setattr("ranch.hand.HANDS_DIR", h)
    monkeypatch.setattr("ranch.hand._pid_file", lambda name: h / f"{name}.pid")
    monkeypatch.setattr("ranch.hand._stop_file", lambda name: h / f"{name}.stop")
    yield h


# ─── Helpers ──────────────────────────────────────────────────────


def _make_run(agent: str, ticket: str, state: str = "planning",
              ended_at: datetime | None = None) -> int:
    init_db()
    with db_session() as db:
        run = Run(agent=agent, ticket=ticket, cwd="/tmp", initial_prompt="x",
                  state=state, ended_at=ended_at)
        db.add(run)
        db.flush()
        return run.id


def _add_dossier(run_id: int, state: str, **extra):
    init_db()
    with db_session() as db:
        payload = {"plan": [{"step": "x", "status": "done"}],
                   "just_did": "did the thing", "state": state, **extra}
        db.add(Dossier(run_id=run_id, state=state,
                        payload_json=json.dumps(payload)))


# ─── Run lookups ──────────────────────────────────────────────────


def test_active_run_returns_none_when_nothing_active():
    init_db()
    assert _active_run_for("max") is None


def test_active_run_finds_non_terminal_run():
    rid = _make_run("max", "ECD-1", state="in_development")
    snap = _active_run_for("max")
    assert snap is not None
    assert snap.id == rid


def test_active_run_excludes_terminal_states():
    _make_run("max", "ECD-1", state="completed")
    _make_run("max", "ECD-2", state="error")
    assert _active_run_for("max") is None


def test_active_run_is_agent_scoped():
    _make_run("jeffy", "ECD-J", state="planning")
    assert _active_run_for("max") is None


def test_last_parked_run_recognizes_parked_dossier_on_terminal_run():
    rid = _make_run("max", "ECD-1", state="completed",
                    ended_at=datetime.now(timezone.utc))
    _add_dossier(rid, "planning")
    _add_dossier(rid, "parked", blocker="Awaiting approval")
    parked = _last_parked_run_for("max")
    assert parked is not None
    assert parked.id == rid


def test_last_parked_run_ignores_non_parked_terminal_runs():
    rid = _make_run("max", "ECD-1", state="completed",
                    ended_at=datetime.now(timezone.utc))
    _add_dossier(rid, "coding")  # ended without parking
    assert _last_parked_run_for("max") is None


def test_last_parked_run_respects_window():
    """A propose from 30 hours ago is stale — operator already saw it or won't."""
    rid = _make_run("max", "ECD-1", state="completed",
                    ended_at=datetime.now(timezone.utc) - timedelta(hours=30))
    _add_dossier(rid, "parked", blocker="x")
    assert _last_parked_run_for("max") is None


# ─── Stop signal handling ────────────────────────────────────────


def test_request_stop_returns_false_when_no_pid_file():
    assert request_stop("max") is False


def test_request_stop_writes_sentinel_when_pid_exists(isolated_hands_dir):
    pid_path = isolated_hands_dir / "max.pid"
    pid_path.write_text("12345")
    assert request_stop("max") is True
    assert (isolated_hands_dir / "max.stop").exists()


# ─── Scheduler decision loop ─────────────────────────────────────


@pytest.mark.asyncio
async def test_hand_picks_top_triage_and_runs_scope_then_propose(tmp_path):
    """The happy path: no active work → triage returns a key → scope + propose fire."""
    called: dict[str, list[str]] = {"triage": [], "scope": [], "propose": []}
    cycles = {"n": 0}

    def triage_fn(_project):
        called["triage"].append("called")
        return ["ECD-100", "ECD-200"]

    def scope_fn(key):
        called["scope"].append(key)

    async def propose_fn(key):
        called["propose"].append(key)
        # Simulate the side effect: a run + parked dossier exist after propose
        rid = _make_run("testhand", key, state="completed",
                        ended_at=datetime.now(timezone.utc))
        _add_dossier(rid, "parked", blocker="Awaiting approval")

    hand = RanchHand("testhand", tmp_path, poll_seconds=0.01,
                      triage_fn=triage_fn, scope_fn=scope_fn, propose_fn=propose_fn)

    # Stop after one iteration to avoid an infinite loop in tests.
    async def stop_after_first_cycle():
        await asyncio.sleep(0.02)
        hand.stop_requested = True

    init_db()
    await asyncio.gather(hand.run(), stop_after_first_cycle())

    assert called["triage"] == ["called"]
    assert called["scope"] == ["ECD-100"]  # top of the ranking
    assert called["propose"] == ["ECD-100"]


@pytest.mark.asyncio
async def test_hand_skips_triage_when_active_run_exists(tmp_path):
    """If there's already an active run, leave it alone."""
    _make_run("testhand", "ECD-7", state="in_development")
    called: dict[str, int] = {"triage": 0, "scope": 0, "propose": 0}

    def triage_fn(_p): called["triage"] += 1; return ["ECD-1"]
    def scope_fn(_k): called["scope"] += 1
    async def propose_fn(_k): called["propose"] += 1

    hand = RanchHand("testhand", tmp_path, poll_seconds=0.01,
                      triage_fn=triage_fn, scope_fn=scope_fn, propose_fn=propose_fn)

    async def stop():
        await asyncio.sleep(0.05)
        hand.stop_requested = True

    await asyncio.gather(hand.run(), stop())
    assert called["triage"] == 0
    assert called["scope"] == 0
    assert called["propose"] == 0


@pytest.mark.asyncio
async def test_hand_skips_triage_when_recent_parked_run_exists(tmp_path):
    """If the previous propose just parked, wait for human review — don't pile on."""
    rid = _make_run("testhand", "ECD-7", state="completed",
                    ended_at=datetime.now(timezone.utc))
    _add_dossier(rid, "parked", blocker="x")

    called = {"triage": 0}
    def triage_fn(_p): called["triage"] += 1; return ["ECD-1"]

    hand = RanchHand("testhand", tmp_path, poll_seconds=0.01, triage_fn=triage_fn)

    async def stop():
        await asyncio.sleep(0.05)
        hand.stop_requested = True

    await asyncio.gather(hand.run(), stop())
    assert called["triage"] == 0


@pytest.mark.asyncio
async def test_hand_handles_empty_triage_gracefully(tmp_path):
    """Empty triage shouldn't crash — just sleep + retry next cycle."""
    cycles = {"triage": 0}
    def triage_fn(_p): cycles["triage"] += 1; return []

    hand = RanchHand("testhand", tmp_path, poll_seconds=0.01, triage_fn=triage_fn)
    async def stop():
        await asyncio.sleep(0.05)
        hand.stop_requested = True

    init_db()
    await asyncio.gather(hand.run(), stop())
    assert cycles["triage"] >= 1  # tried at least once, didn't crash


@pytest.mark.asyncio
async def test_hand_handles_triage_exception_gracefully(tmp_path):
    """If triage raises, log + continue."""
    def triage_fn(_p): raise RuntimeError("jira down")

    hand = RanchHand("testhand", tmp_path, poll_seconds=0.01, triage_fn=triage_fn)
    async def stop():
        await asyncio.sleep(0.05)
        hand.stop_requested = True

    init_db()
    await asyncio.gather(hand.run(), stop())  # must not raise


@pytest.mark.asyncio
async def test_stop_signal_file_terminates_loop(tmp_path, isolated_hands_dir):
    """Touching the .stop sentinel exits cleanly between cycles."""
    triggered = {"n": 0}
    def triage_fn(_p):
        triggered["n"] += 1
        # Drop the stop sentinel as a side effect of the first triage call
        (isolated_hands_dir / "testhand.stop").touch()
        return []

    hand = RanchHand("testhand", tmp_path, poll_seconds=0.01, triage_fn=triage_fn)
    init_db()
    await hand.run()
    assert triggered["n"] >= 1
    # Sentinel is consumed by the loop
    assert not (isolated_hands_dir / "testhand.stop").exists()


# ─── Status snapshots ────────────────────────────────────────────


def test_status_when_no_pid_returns_stopped(isolated_hands_dir):
    s = get_hand_status("max")
    assert s.state == "stopped"
    assert s.pid is None


def test_status_when_pid_alive_and_active_run(isolated_hands_dir):
    import os
    (isolated_hands_dir / "max.pid").write_text(str(os.getpid()))
    rid = _make_run("max", "ECD-1", state="in_development")
    _add_dossier(rid, "coding")

    s = get_hand_status("max")
    assert s.state == "running"
    assert s.current_run_id == rid
    assert s.current_ticket == "ECD-1"
    assert s.current_dossier_state == "coding"


def test_status_when_pid_alive_and_parked(isolated_hands_dir):
    import os
    (isolated_hands_dir / "max.pid").write_text(str(os.getpid()))
    rid = _make_run("max", "ECD-1", state="completed",
                    ended_at=datetime.now(timezone.utc))
    _add_dossier(rid, "parked", blocker="x")

    s = get_hand_status("max")
    assert s.current_dossier_state == "parked"
    assert "awaiting review" in s.detail


def test_status_when_pid_alive_no_work(isolated_hands_dir):
    import os
    (isolated_hands_dir / "max.pid").write_text(str(os.getpid()))
    init_db()
    s = get_hand_status("max")
    assert s.state == "running"
    assert "idle" in s.detail


def test_status_when_pid_stale_returns_missing(isolated_hands_dir):
    # A pid that's almost certainly not alive
    (isolated_hands_dir / "max.pid").write_text("999999")
    s = get_hand_status("max")
    assert s.state == "missing"
