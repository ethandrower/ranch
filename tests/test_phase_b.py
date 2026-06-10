"""Phase B (issue #110) tests — hand pickup respects routing label + blocks.

Verifies that:
1. `_default_triage` routes via `list_for_hand` (Phase A v2 label)
2. `_find_approved_parked_propose` skips runs with unresolved blocks even
   when the operator has queued an approve interjection
3. The block cascade still works end-to-end: approve the blocker → block
   resolves → dependent run becomes pickup-eligible on next tick
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from ranch.blocks import record_block, resolve_blocks_on_approve
from ranch.db import db_session
from ranch.hand import RanchHand, _find_approved_parked_propose
from ranch.models import Block, Dossier, Interjection, Run


def _add_run(s, **kw) -> Run:
    defaults = dict(
        agent="max", state="completed", cwd="/tmp", initial_prompt="x",
        ended_at=datetime.now(timezone.utc) - timedelta(minutes=2),
    )
    defaults.update(kw)
    r = Run(**defaults)
    s.add(r); s.flush()
    return r


def _park_with_plan(s, run_id: int, ticket: str = "ECD-1") -> None:
    """Drop a parked dossier on a run."""
    s.add(Dossier(
        run_id=run_id, state="parked",
        payload_json=json.dumps({
            "plan": [{"step": "draft plan", "status": "done"}],
            "just_did": "drafted plan; parked at plan_ready",
            "state": "parked",
            "ticket": ticket,
        }),
    ))


# ─── _find_approved_parked_propose: happy path (no block) ──────────


def test_find_approved_propose_fires_when_unblocked():
    """Sanity baseline: with no block, an approved parked propose IS picked up."""
    with db_session() as s:
        r = _add_run(s, ticket="ECD-100")
        _park_with_plan(s, r.id, "ECD-100")
        s.add(Interjection(run_id=r.id, kind="approve", content=""))

    result = _find_approved_parked_propose("max")
    assert result is not None
    assert result.propose_run.id == r.id

    # The approve interjection should be marked processed
    with db_session() as s:
        ij = s.query(Interjection).filter_by(run_id=r.id).one()
        assert ij.processed_at is not None


# ─── _find_approved_parked_propose: block defers execute ───────────


def test_find_approved_propose_skips_blocked_run():
    """Approve on a blocked propose is held — interjection stays
    unprocessed so it can fire once the block resolves."""
    with db_session() as s:
        r = _add_run(s, ticket="ECD-200")
        _park_with_plan(s, r.id, "ECD-200")
        s.add(Interjection(run_id=r.id, kind="approve", content="lgtm"))

    record_block(blocked_run_id=r.id, blocker_ticket="ECD-199",
                 reason="depends on ECD-199's plan call")

    result = _find_approved_parked_propose("max")
    assert result is None

    # The approve interjection must STILL be unprocessed
    with db_session() as s:
        ij = s.query(Interjection).filter_by(run_id=r.id).one()
        assert ij.processed_at is None


def test_blocked_propose_fires_after_unblock():
    """End-to-end cascade: blocker gets approved → block resolves →
    dependent's existing approve interjection fires on next tick."""
    with db_session() as s:
        # The blocker
        blocker = _add_run(s, ticket="ECD-300", state="parked",
                          ended_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        # The dependent — also parked, with an approve queued
        dependent = _add_run(s, ticket="ECD-301")
        _park_with_plan(s, dependent.id, "ECD-301")
        s.add(Interjection(run_id=dependent.id, kind="approve", content=""))

    record_block(blocked_run_id=dependent.id, blocker_ticket="ECD-300",
                 reason="r")

    # Before unblock — held
    assert _find_approved_parked_propose("max") is None

    # Simulate operator approving the blocker → cascade resolves the block
    n = resolve_blocks_on_approve(blocker.id)
    assert n == 1

    # Next tick — now the dependent fires
    result = _find_approved_parked_propose("max")
    assert result is not None
    assert result.propose_run.id == dependent.id


def test_multiple_blocked_runs_only_unblocked_one_fires():
    """If two propose runs are both approved-but-blocked-by-different-tickets,
    resolving only one block should fire only the one whose blocker cleared."""
    with db_session() as s:
        blocker_a = _add_run(s, ticket="ECD-400", state="parked",
                            ended_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        # blocker B intentionally not approved
        dep_a = _add_run(s, ticket="ECD-401")
        _park_with_plan(s, dep_a.id, "ECD-401")
        s.add(Interjection(run_id=dep_a.id, kind="approve"))

        dep_b = _add_run(s, ticket="ECD-402",
                        ended_at=datetime.now(timezone.utc) - timedelta(seconds=30))
        _park_with_plan(s, dep_b.id, "ECD-402")
        s.add(Interjection(run_id=dep_b.id, kind="approve"))

    record_block(blocked_run_id=dep_a.id, blocker_ticket="ECD-400", reason="r")
    record_block(blocked_run_id=dep_b.id, blocker_ticket="ECD-999-unrelated", reason="r")

    # Approve blocker A's cascade
    resolve_blocks_on_approve(blocker_a.id)

    # _find_approved_parked_propose returns one at a time. Iterate to drain.
    fired_ticket_keys: list[str] = []
    for _ in range(5):
        result = _find_approved_parked_propose("max")
        if result is None:
            break
        fired_ticket_keys.append(result.propose_run.ticket)

    assert "ECD-401" in fired_ticket_keys
    assert "ECD-402" not in fired_ticket_keys


# ─── _default_triage routing ───────────────────────────────────────


def test_default_triage_routes_via_list_for_hand(monkeypatch):
    """Verify hand.py's triage calls list_for_hand (the routing path),
    not the legacy list_assigned_to_me."""
    from ranch.triage import JiraConfig, JiraClient, JiraTicket

    captured: dict[str, tuple] = {}

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def list_for_hand(self, hand_name, *, assignee_account=None, project=None):
            captured["call"] = ("list_for_hand", hand_name, assignee_account, project)
            return [
                JiraTicket(
                    key="ECD-7", summary="x", status="To Do", status_category="new",
                    priority=None,
                    created=datetime.now(timezone.utc),
                    updated=datetime.now(timezone.utc),
                    description="", labels=["ranch-max"],
                ),
            ]
        def list_assigned_to_me(self, **kw):
            captured["call"] = ("legacy_list_assigned_to_me",)
            return []

    fake_cfg = JiraConfig(url="https://x", email="ethan@citemed.io",
                          api_token="t", hand_account="ethan@citemed.io")

    # _default_triage does a local `from .triage import ...` so we have to
    # patch the actual source module, not ranch.hand.
    monkeypatch.setattr("ranch.triage.JiraClient", lambda c: FakeClient())
    monkeypatch.setattr("ranch.triage.JiraConfig.load", classmethod(lambda cls: fake_cfg))

    from pathlib import Path
    hand = RanchHand(name="max", cwd=Path("/tmp"))
    keys = hand._default_triage(project=None)

    assert captured["call"][0] == "list_for_hand"
    assert captured["call"][1] == "max"
    assert captured["call"][2] == "ethan@citemed.io"
    assert "ECD-7" in keys
