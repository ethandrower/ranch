"""Tests for per-step details projection (ranch.view.step_details)."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from ranch.api.app import create_app
from ranch.db import db_session
from ranch.models import Dossier, Run
from ranch.view.step_details import step_details_for_run, step_details_for_ticket


def _add_run(s, **kw) -> Run:
    defaults = dict(agent="max", state="parked", cwd="/tmp", initial_prompt="x")
    defaults.update(kw)
    r = Run(**defaults)
    s.add(r); s.flush()
    return r


def _add_dossier(s, run_id: int, plan: list[dict], details: str = "", state: str = "coding") -> None:
    s.add(Dossier(run_id=run_id, state=state, payload_json=json.dumps({
        "plan": plan, "just_did": "x", "state": state, "details": details,
    })))
    s.flush()


def test_first_done_capture_locks_in_details():
    """Once a step is marked done, later writes don't overwrite its details."""
    with db_session() as s:
        r = _add_run(s, ticket="ECD-1")
        _add_dossier(s, r.id, [{"step": "A", "status": "in_progress"}], details="early")
        _add_dossier(s, r.id, [{"step": "A", "status": "done"}], details="A-finished")
        _add_dossier(s, r.id, [{"step": "A", "status": "done"}, {"step": "B", "status": "in_progress"}], details="working on B")
    out = step_details_for_run(r.id)
    assert out == {"A": "A-finished"}


def test_each_step_gets_its_transition_point_details():
    with db_session() as s:
        r = _add_run(s, ticket="ECD-2")
        _add_dossier(s, r.id, [
            {"step": "A", "status": "in_progress"},
            {"step": "B", "status": "pending"},
        ], details="started A")
        _add_dossier(s, r.id, [
            {"step": "A", "status": "done"},
            {"step": "B", "status": "in_progress"},
        ], details="finished A — wrote the parser")
        _add_dossier(s, r.id, [
            {"step": "A", "status": "done"},
            {"step": "B", "status": "done"},
        ], details="wired B")
    out = step_details_for_run(r.id)
    assert out == {"A": "finished A — wrote the parser", "B": "wired B"}


def test_step_with_no_details_returns_empty_string():
    """If the dossier at the transition point had no details, we still
    record an entry (empty string) so the UI can distinguish 'step
    completed but no details captured' from 'step not done'."""
    with db_session() as s:
        r = _add_run(s, ticket="ECD-3")
        _add_dossier(s, r.id, [{"step": "Q", "status": "done"}])  # no details
    out = step_details_for_run(r.id)
    assert out == {"Q": ""}


def test_pending_steps_not_included():
    with db_session() as s:
        r = _add_run(s, ticket="ECD-4")
        _add_dossier(s, r.id, [
            {"step": "A", "status": "done"},
            {"step": "B", "status": "in_progress"},
        ], details="A done")
    out = step_details_for_run(r.id)
    assert out == {"A": "A done"}
    assert "B" not in out


def test_step_details_for_ticket_picks_active_run():
    with db_session() as s:
        from datetime import datetime, timezone
        old = _add_run(s, ticket="ECD-5", state="completed",
                       ended_at=datetime.now(timezone.utc))
        _add_dossier(s, old.id, [{"step": "X", "status": "done"}], details="old")
        s.flush()
        active = _add_run(s, ticket="ECD-5")
        _add_dossier(s, active.id, [{"step": "X", "status": "done"}], details="new")
    out = step_details_for_ticket("ECD-5")
    # active run wins
    assert out == {"X": "new"}


def test_step_details_for_ticket_no_runs_returns_empty():
    assert step_details_for_ticket("ECD-NONE") == {}


def test_api_endpoint_returns_step_details():
    with db_session() as s:
        r = _add_run(s, ticket="ECD-9")
        _add_dossier(s, r.id, [{"step": "A", "status": "done"}], details="d1")

    client = TestClient(create_app())
    rsp = client.get("/api/tickets/ECD-9/step-details")
    assert rsp.status_code == 200
    assert rsp.json() == {"A": "d1"}
