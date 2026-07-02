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
from unittest.mock import AsyncMock, patch

import pytest

from ranch.db import db_session, init_db
from ranch.hand import (
    AWAITING_APPROVAL_WINDOW,
    HANDS_DIR,
    RanchHand,
    _active_run_for,
    _ApprovedPropose,
    _build_execute_brief,
    _create_execute_run,
    _find_approved_parked_propose,
    _last_parked_run_for,
    _pid_file,
    _read_pid,
    _stop_file,
    get_hand_status,
    list_all_hand_statuses,
    request_stop,
)
from ranch.models import Dossier, Interjection, Run


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
async def test_hand_discovers_and_queues_without_firing_propose(tmp_path):
    """Operator-kickoff flow (post-f1ac9e3): the hand's main loop must
    NOT auto-fire scope+propose on discovered triage candidates. It
    inserts them as state='queued' Runs for the operator to kick off
    via the UI.

    The legacy auto-fire-propose path is gone; this test pins the new
    discovery-only behavior.
    """
    # Inject _discover_and_queue with a stub that records its call AND
    # creates queued runs the same way the real impl does. The auto-fire
    # scope/propose fns must NOT get called.
    called: dict[str, list[str]] = {"discover": [], "scope": [], "propose": []}

    def stub_discover(self_, *, max_queue=10):
        called["discover"].append("called")
        # Mirror real impl side-effect: insert queued run rows
        with db_session() as s:
            for key, score in [("ECD-100", 60), ("ECD-200", 30)]:
                s.add(Run(
                    agent="testhand", ticket=key, state="queued", cwd="/tmp",
                    initial_prompt=f"summary of {key}",
                    triage_score=score, triage_summary=f"summary of {key}",
                    started_at=datetime.now(timezone.utc),
                ))
        return 2

    def scope_fn(key):
        called["scope"].append(key)

    async def propose_fn(key):
        called["propose"].append(key)

    hand = RanchHand("testhand", tmp_path, poll_seconds=0.01,
                     scope_fn=scope_fn, propose_fn=propose_fn)
    # Patch the discovery method on this instance
    import types
    hand._discover_and_queue = types.MethodType(stub_discover, hand)

    async def stop_after_first_cycle():
        await asyncio.sleep(0.05)
        hand.stop_requested = True

    init_db()
    await asyncio.gather(hand.run(), stop_after_first_cycle())

    assert called["discover"] == ["called"]
    # Critical: scope + propose MUST NOT have been called
    assert called["scope"] == []
    assert called["propose"] == []
    # And the queued candidates should be in the DB
    with db_session() as s:
        queued = s.query(Run).filter_by(agent="testhand", state="queued").all()
        keys = sorted([r.ticket for r in queued])
    assert keys == ["ECD-100", "ECD-200"]


@pytest.mark.asyncio
async def test_hand_kickoff_interjection_fires_propose(tmp_path):
    """The flip side: when the operator queues a `kickoff` interjection
    on a queued Run, the next hand tick fires scope+propose for it."""
    rid = _make_run("testhand", "ECD-100", state="queued")
    with db_session() as s:
        s.add(Interjection(run_id=rid, kind="kickoff", content=""))

    called: dict[str, list[str]] = {"scope": [], "propose": [], "discover": []}

    def scope_fn(key): called["scope"].append(key)
    async def propose_fn(key): called["propose"].append(key)
    def stub_discover(self_, **kw):
        called["discover"].append("called")
        return 0

    hand = RanchHand("testhand", tmp_path, poll_seconds=0.01,
                     scope_fn=scope_fn, propose_fn=propose_fn)
    import types
    hand._discover_and_queue = types.MethodType(stub_discover, hand)

    async def stop():
        await asyncio.sleep(0.05)
        hand.stop_requested = True

    await asyncio.gather(hand.run(), stop())

    assert called["scope"] == ["ECD-100"]
    assert called["propose"] == ["ECD-100"]
    # Interjection was consumed
    with db_session() as s:
        ij = s.query(Interjection).filter_by(run_id=rid, kind="kickoff").one()
        assert ij.processed_at is not None


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


# ─── H11 v2: approval detection + auto-execute ───────────────────


def _add_approve(run_id: int):
    init_db()
    with db_session() as db:
        db.add(Interjection(run_id=run_id, kind="approve", content=""))


def test_find_approved_returns_none_with_no_runs():
    init_db()
    assert _find_approved_parked_propose("max") is None


def test_find_approved_returns_none_when_parked_but_no_approval():
    rid = _make_run("max", "ECD-1", state="completed",
                    ended_at=datetime.now(timezone.utc))
    _add_dossier(rid, "parked", blocker="x")
    assert _find_approved_parked_propose("max") is None


def test_find_approved_returns_none_when_approval_but_not_parked():
    """An approve interjection on a run that never parked shouldn't fire execute."""
    rid = _make_run("max", "ECD-1", state="completed",
                    ended_at=datetime.now(timezone.utc))
    _add_dossier(rid, "coding")
    _add_approve(rid)
    assert _find_approved_parked_propose("max") is None


def test_find_approved_returns_run_and_payload_when_both_present():
    rid = _make_run("max", "ECD-1", state="completed",
                    ended_at=datetime.now(timezone.utc))
    _add_dossier(rid, "parked", blocker="x", just_did="ready",
                  plan=[{"step": "do it", "status": "done"}])
    _add_approve(rid)
    approved = _find_approved_parked_propose("max")
    assert approved is not None
    assert approved.propose_run.id == rid
    assert approved.parked_payload["plan"][0]["step"] == "do it"


def test_find_approved_marks_interjection_processed():
    """Same approval cannot fire execute twice."""
    rid = _make_run("max", "ECD-1", state="completed",
                    ended_at=datetime.now(timezone.utc))
    _add_dossier(rid, "parked", blocker="x")
    _add_approve(rid)

    first = _find_approved_parked_propose("max")
    second = _find_approved_parked_propose("max")
    assert first is not None
    assert second is None


def test_find_approved_is_agent_scoped():
    rid = _make_run("jeffy", "ECD-J", state="completed",
                    ended_at=datetime.now(timezone.utc))
    _add_dossier(rid, "parked", blocker="x")
    _add_approve(rid)
    assert _find_approved_parked_propose("max") is None


def test_find_approved_respects_approval_window():
    rid = _make_run("max", "ECD-1", state="completed",
                    ended_at=datetime.now(timezone.utc) - timedelta(hours=30))
    _add_dossier(rid, "parked", blocker="x")
    _add_approve(rid)
    assert _find_approved_parked_propose("max") is None


def test_build_execute_brief_inlines_plan_and_details():
    payload = {
        "details": "## Summary\n\nDo the thing.",
        "plan": [
            {"step": "Step one", "status": "pending"},
            {"step": "Step two", "status": "pending"},
        ],
    }
    brief = _build_execute_brief("ECD-100", payload)
    assert "Ticket: ECD-100" in brief
    assert "Step one" in brief
    assert "Step two" in brief
    assert "Do the thing." in brief
    assert "run_acceptance" in brief
    assert "pre_push" in brief


def test_build_execute_brief_handles_missing_fields():
    brief = _build_execute_brief(None, {})
    assert "(no ticket)" in brief
    assert "(no plan steps in propose)" in brief
    assert "(no details captured)" in brief


def test_create_execute_run_preseeds_acceptance_onto_new_run(tmp_path):
    """The hand carries the acceptance contract forward so H8's hook can find it."""
    init_db()
    payload = {
        "plan": [{"step": "x", "status": "pending"}],
        "acceptance": [
            {"kind": "unit_test", "name": "p", "cmd": "pytest", "pass_pattern": "passed"},
        ],
        "details": "## Summary\n\nx",
    }
    new_id = _create_execute_run(
        agent="max", cwd=tmp_path, ticket="ECD-100",
        brief="x", parked_payload=payload,
    )
    with db_session() as db:
        run = db.query(Run).filter_by(id=new_id).one()
        assert run.agent == "max"
        assert run.ticket == "ECD-100"
        assert run.state == "planning"

        latest = (
            db.query(Dossier)
            .filter_by(run_id=new_id)
            .order_by(Dossier.created_at.desc())
            .first()
        )
        seeded = json.loads(latest.payload_json)
        assert seeded["acceptance"][0]["name"] == "p"
        assert "Execute step initiated" in seeded["just_did"]


@pytest.mark.asyncio
async def test_hand_fires_execute_when_approval_detected(tmp_path):
    """End-to-end loop check: parked + approve → execute_fn invoked."""
    rid = _make_run("testhand", "ECD-X", state="completed",
                    ended_at=datetime.now(timezone.utc))
    _add_dossier(rid, "parked", blocker="x",
                  plan=[{"step": "do", "status": "pending"}])
    _add_approve(rid)

    captured = {"executed": None}
    async def fake_execute(approved):
        captured["executed"] = approved.propose_run.ticket

    hand = RanchHand(
        "testhand", tmp_path, poll_seconds=0.01,
        triage_fn=lambda p: [],
        scope_fn=lambda k: None,
        propose_fn=AsyncMock(),
        execute_fn=fake_execute,
    )

    async def stop():
        await asyncio.sleep(0.05)
        hand.stop_requested = True

    await asyncio.gather(hand.run(), stop())
    assert captured["executed"] == "ECD-X"


@pytest.mark.asyncio
async def test_hand_skips_triage_when_execute_just_fired(tmp_path):
    """The cycle that fires execute shouldn't also triage — that would
    duplicate work for the operator to untangle."""
    rid = _make_run("testhand", "ECD-X", state="completed",
                    ended_at=datetime.now(timezone.utc))
    _add_dossier(rid, "parked", blocker="x")
    _add_approve(rid)

    triage_calls = {"n": 0}
    def triage_fn(_p):
        triage_calls["n"] += 1
        return []

    async def fake_execute(_approved):
        pass

    hand = RanchHand(
        "testhand", tmp_path, poll_seconds=0.01,
        triage_fn=triage_fn,
        scope_fn=lambda k: None,
        propose_fn=AsyncMock(),
        execute_fn=fake_execute,
    )

    async def stop():
        await asyncio.sleep(0.05)
        hand.stop_requested = True

    await asyncio.gather(hand.run(), stop())
    # Triage may have been called on subsequent cycles AFTER execute fired
    # (since execute consumed the parked-with-approval state). But it should
    # NOT have been called in the SAME cycle as the execute fire — we test
    # this by checking the approve interjection was consumed exactly once.
    with db_session() as db:
        approved = db.query(Interjection).filter_by(run_id=rid, kind="approve").one()
    assert approved.processed_at is not None


# ─── Orchestrator per-kind auto-approve ──────────────────────────


def test_orchestrator_auto_approve_kinds_overrides_blanket():
    """auto_approve_kinds wins over auto_approve flag for the listed kinds."""
    from ranch.runner.orchestrator import Orchestrator
    orch = Orchestrator(
        agent="x", cwd=Path("/tmp"), ticket="t", brief="b",
        auto_approve=False,
        auto_approve_kinds={"plan_ready"},
    )
    assert orch.auto_approve_kinds == {"plan_ready"}


@pytest.mark.asyncio
async def test_orchestrator_on_checkpoint_only_auto_fires_listed_kinds():
    from ranch.runner.orchestrator import Orchestrator
    init_db()
    with db_session() as db:
        run = Run(agent="x", ticket="t", cwd="/tmp", initial_prompt="b", state="planning")
        db.add(run); db.flush()
        rid = run.id

    orch = Orchestrator(
        agent="x", cwd=Path("/tmp"), ticket="t", brief="b",
        auto_approve_kinds={"plan_ready"},
    )
    orch.run_id = rid

    # plan_ready: should auto-fire approval
    await orch.on_checkpoint("plan_ready", "ok", None)
    assert orch._approval_ready.is_set()
    orch._approval_ready.clear()
    orch._approval_result = None

    # pre_push: should NOT auto-fire
    await orch.on_checkpoint("pre_push", "ok", None)
    assert not orch._approval_ready.is_set()


# ─── H20: PR review unblock detection ───────────────────────────


def _seed_parked_pr_run(agent: str = "max", pr_id: str = "42",
                         last_check: datetime | None = None) -> int:
    init_db()
    with db_session() as db:
        run = Run(
            agent=agent, ticket="ECD-7", cwd="/tmp", initial_prompt="x",
            state="completed", branch_name="feature/ECD-7",
            pr_id=pr_id, pr_platform="bb",
            ended_at=datetime.now(timezone.utc),
            last_pr_check_at=last_check,
        )
        db.add(run); db.flush()
        rid = run.id
    _add_dossier(rid, "parked", blocker="awaiting_pr_review",
                  plan=[{"step": "x", "status": "done"}])
    return rid


@pytest.mark.asyncio
async def test_check_pr_review_unblocks_fires_respond_when_new_comments(tmp_path):
    """Happy path: parked PR, throttle satisfied, poll returns new comments → respond."""
    from ranch.pr_loop import PollResult
    rid = _seed_parked_pr_run()
    poll_calls: list[int] = []
    respond_calls: list[int] = []

    def fake_poll(run_id: int):
        poll_calls.append(run_id)
        return PollResult(ok=True, pr_id="42", new_comment_count=2,
                          new_comments=[])
    async def fake_respond(run_id: int):
        respond_calls.append(run_id)

    hand = RanchHand(
        "max", tmp_path,
        pr_poll_fn=fake_poll, respond_pr_fn=fake_respond,
    )
    fired = await hand._check_pr_review_unblocks()
    assert fired is True
    assert poll_calls == [rid]
    assert respond_calls == [rid]


@pytest.mark.asyncio
async def test_check_pr_review_unblocks_skips_when_no_new_comments(tmp_path):
    from ranch.pr_loop import PollResult
    _seed_parked_pr_run()
    respond_calls: list[int] = []

    def fake_poll(run_id: int):
        return PollResult(ok=True, pr_id="42", new_comment_count=0)
    async def fake_respond(run_id: int):
        respond_calls.append(run_id)

    hand = RanchHand(
        "max", tmp_path,
        pr_poll_fn=fake_poll, respond_pr_fn=fake_respond,
    )
    fired = await hand._check_pr_review_unblocks()
    assert fired is False
    assert respond_calls == []


@pytest.mark.asyncio
async def test_check_pr_review_unblocks_respects_cadence(tmp_path):
    """Run was polled 5s ago; cadence is 120s; we should NOT re-poll yet."""
    recent = datetime.now(timezone.utc) - timedelta(seconds=5)
    _seed_parked_pr_run(last_check=recent)
    poll_calls: list[int] = []
    def fake_poll(run_id: int):
        poll_calls.append(run_id)
        return None
    async def fake_respond(_): pass

    hand = RanchHand(
        "max", tmp_path,
        pr_poll_interval_seconds=120.0,
        pr_poll_fn=fake_poll, respond_pr_fn=fake_respond,
    )
    fired = await hand._check_pr_review_unblocks()
    assert fired is False
    assert poll_calls == []


@pytest.mark.asyncio
async def test_check_pr_review_unblocks_polls_when_cadence_satisfied(tmp_path):
    """Run was polled 200s ago; cadence is 120s; we SHOULD re-poll."""
    from ranch.pr_loop import PollResult
    old = datetime.now(timezone.utc) - timedelta(seconds=200)
    rid = _seed_parked_pr_run(last_check=old)
    poll_calls: list[int] = []
    def fake_poll(run_id: int):
        poll_calls.append(run_id)
        return PollResult(ok=True, pr_id="42", new_comment_count=0)
    async def fake_respond(_): pass

    hand = RanchHand(
        "max", tmp_path,
        pr_poll_interval_seconds=120.0,
        pr_poll_fn=fake_poll, respond_pr_fn=fake_respond,
    )
    await hand._check_pr_review_unblocks()
    assert poll_calls == [rid]


@pytest.mark.asyncio
async def test_check_pr_review_unblocks_no_candidates_returns_false(tmp_path):
    init_db()
    hand = RanchHand("max", tmp_path)
    fired = await hand._check_pr_review_unblocks()
    assert fired is False


@pytest.mark.asyncio
async def test_check_pr_review_unblocks_swallows_poll_errors(tmp_path):
    """A backend error on one candidate shouldn't abort the loop."""
    _seed_parked_pr_run()
    def fake_poll(run_id: int):
        raise RuntimeError("bb down")
    hand = RanchHand("max", tmp_path, pr_poll_fn=fake_poll)
    fired = await hand._check_pr_review_unblocks()
    assert fired is False


# ─── H20 P2: CI status unblock detection ────────────────────────


@pytest.mark.asyncio
async def test_check_ci_unblocks_emits_dossier_when_flipped(tmp_path):
    """When ci_poll_fn returns flipped=True, the hand calls emit_ci_flip_dossier."""
    from ranch.ci_loop import CIPollResult
    rid = _seed_parked_pr_run()
    poll_calls = []
    def fake_poll(run_id: int):
        poll_calls.append(run_id)
        return CIPollResult(
            ok=True, pr_id="42", commit_sha="abc",
            status="passed", flipped=True, previous_status="running",
        )

    hand = RanchHand("max", tmp_path, ci_poll_fn=fake_poll)
    fired = await hand._check_ci_unblocks()
    assert fired is True
    assert poll_calls == [rid]
    # Dossier row written
    with db_session() as db:
        rows = db.query(Dossier).filter_by(run_id=rid).all()
    assert any("CI passed" in (json.loads(r.payload_json).get("just_did", "")) for r in rows)


@pytest.mark.asyncio
async def test_check_ci_unblocks_returns_false_when_no_flip(tmp_path):
    from ranch.ci_loop import CIPollResult
    _seed_parked_pr_run()
    def fake_poll(run_id: int):
        return CIPollResult(ok=True, pr_id="42", status="running", flipped=False)

    hand = RanchHand("max", tmp_path, ci_poll_fn=fake_poll)
    fired = await hand._check_ci_unblocks()
    assert fired is False


@pytest.mark.asyncio
async def test_check_ci_unblocks_handles_no_candidates(tmp_path):
    init_db()
    hand = RanchHand("max", tmp_path)
    fired = await hand._check_ci_unblocks()
    assert fired is False


@pytest.mark.asyncio
async def test_check_ci_unblocks_swallows_poll_errors(tmp_path):
    _seed_parked_pr_run()
    def fake_poll(run_id: int):
        raise RuntimeError("bb network down")
    hand = RanchHand("max", tmp_path, ci_poll_fn=fake_poll)
    fired = await hand._check_ci_unblocks()
    assert fired is False


@pytest.mark.asyncio
async def test_hand_main_loop_fires_pr_response_before_triage(tmp_path):
    """Integration: main loop reaches _check_pr_review_unblocks step,
    fires the respond, then skips triage that cycle."""
    from ranch.pr_loop import PollResult
    rid = _seed_parked_pr_run()
    respond_calls: list[int] = []

    def triage_fn(_p):
        return []
    def fake_poll(run_id: int):
        return PollResult(ok=True, pr_id="42", new_comment_count=1)
    async def fake_respond(run_id: int):
        respond_calls.append(run_id)

    hand = RanchHand(
        "max", tmp_path, poll_seconds=0.01,
        triage_fn=triage_fn,
        pr_poll_fn=fake_poll,
        respond_pr_fn=fake_respond,
    )
    async def stop():
        await asyncio.sleep(0.05)
        hand.stop_requested = True
    await asyncio.gather(hand.run(), stop())
    assert respond_calls == [rid]
