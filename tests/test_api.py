"""Tests for the FastAPI sidecar.

Use FastAPI's TestClient — synchronous, doesn't need a real port. The SSE
endpoint is exercised at the publish/subscribe level (test_api_events.py)
rather than over the network because TestClient + streaming + asyncio
queues is finicky.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from ranch.api.app import create_app
from ranch.blocks import record_block
from ranch.db import db_session
from ranch.models import (
    Block,
    Checkpoint,
    Dossier,
    HandInitiative,
    Initiative,
    Interjection,
    Run,
)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _add_initiative(s, key: str, label: str, hand: str, *, is_default: int = 1) -> None:
    s.add(Initiative(key=key, label=label))
    s.flush()
    s.add(HandInitiative(hand_name=hand, initiative_key=key, is_default=is_default))


def _add_run(s, **kw) -> Run:
    defaults = dict(agent="max", state="queued", cwd="/tmp", initial_prompt="x")
    defaults.update(kw)
    r = Run(**defaults)
    s.add(r); s.flush()
    return r


# ─── /api/health ──────────────────────────────────────────────────


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ─── /api/hands ────────────────────────────────────────────────────


def test_list_hands_empty(client):
    r = client.get("/api/hands")
    assert r.status_code == 200
    assert r.json() == []


def test_list_hands_returns_summary_for_each_known_hand(client):
    with db_session() as s:
        _add_initiative(s, "ref-mgmt", "Reference Management", "max")
        _add_run(s, agent="max", ticket="ECD-1", state="in_development")
        _add_run(s, agent="max", ticket="ECD-2", state="needs_approval")
        _add_run(s, agent="jeffy", ticket="ECD-3", state="queued")
    r = client.get("/api/hands")
    assert r.status_code == 200
    data = r.json()
    names = {h["name"] for h in data}
    assert names == {"max", "jeffy"}
    max_entry = next(h for h in data if h["name"] == "max")
    assert max_entry["ticket_count"] == 2
    # needs_approval = attention
    assert max_entry["attention_count"] >= 1


def test_get_hand_returns_full_view_model(client):
    with db_session() as s:
        _add_initiative(s, "ref-mgmt", "Reference Management", "max")
        _add_run(s, ticket="ECD-100", state="in_development")
    r = client.get("/api/hands/max")
    assert r.status_code == 200
    view = r.json()
    assert view["label"] == "max"
    assert view["initiatives"] == ["ref-mgmt"]
    assert len(view["tickets"]) == 1
    assert view["tickets"][0]["key"] == "ECD-100"


def test_get_hand_for_unknown_hand_returns_empty_shell(client):
    r = client.get("/api/hands/nobody")
    assert r.status_code == 200
    v = r.json()
    assert v["tickets"] == []
    assert v["status"] == "idle"


# ─── /api/tickets/{key} ───────────────────────────────────────────


def test_get_ticket_returns_view_with_run_id(client):
    with db_session() as s:
        run = _add_run(s, ticket="ECD-500", state="in_development")
        s.add(Dossier(run_id=run.id, state="coding", payload_json=json.dumps({
            "plan": [{"step": "do thing", "status": "done"}],
            "just_did": "made progress",
            "state": "coding",
        })))
    r = client.get("/api/tickets/ECD-500")
    assert r.status_code == 200
    v = r.json()
    assert v["key"] == "ECD-500"
    assert v["stage"] == "code"
    assert v["done"] == ["do thing"]
    assert "run_id" in v


def test_get_ticket_404_when_no_run(client):
    r = client.get("/api/tickets/ECD-NOPE")
    assert r.status_code == 404


def test_get_ticket_falls_back_to_ended_run(client):
    from datetime import datetime, timezone
    with db_session() as s:
        _add_run(s, ticket="ECD-OLD", state="merged",
                 ended_at=datetime.now(timezone.utc))
    r = client.get("/api/tickets/ECD-OLD")
    assert r.status_code == 200
    assert r.json()["stage"] == "merge"


# ─── /api/runs/{id}/approve ───────────────────────────────────────


def test_approve_queues_interjection(client):
    with db_session() as s:
        r = _add_run(s, ticket="ECD-700", state="needs_approval")
    rsp = client.post(f"/api/runs/{r.id}/approve", json={"text": "lgtm"})
    assert rsp.status_code == 200
    assert rsp.json()["ok"] is True
    with db_session() as s:
        rows = s.query(Interjection).filter_by(run_id=r.id, kind="approve").all()
        assert len(rows) == 1
        assert rows[0].content == "lgtm"


def test_approve_404_for_unknown_run(client):
    rsp = client.post("/api/runs/99999/approve")
    assert rsp.status_code == 404


def test_approve_cascade_unblocks_dependents(client):
    with db_session() as s:
        blocker = _add_run(s, ticket="ECD-800", state="parked")
        dep = _add_run(s, ticket="ECD-801")
    record_block(blocked_run_id=dep.id, blocker_ticket="ECD-800", reason="r")

    rsp = client.post(f"/api/runs/{blocker.id}/approve")
    assert rsp.status_code == 200
    assert rsp.json()["unblocked"] == 1

    with db_session() as s:
        block = s.query(Block).filter_by(blocked_run_id=dep.id).one()
        assert block.resolved_at is not None


# ─── /api/runs/{id}/reject|note|stop ──────────────────────────────


def test_reject_queues_interjection(client):
    with db_session() as s:
        r = _add_run(s, ticket="ECD-900", state="needs_approval")
    rsp = client.post(f"/api/runs/{r.id}/reject", json={"reason": "scope too wide"})
    assert rsp.status_code == 200
    with db_session() as s:
        row = s.query(Interjection).filter_by(run_id=r.id, kind="reject").one()
        assert row.content == "scope too wide"


def test_note_queues_interjection(client):
    with db_session() as s:
        r = _add_run(s, ticket="ECD-901", state="in_development")
    rsp = client.post(f"/api/runs/{r.id}/note", json={"text": "watch out for the cache"})
    assert rsp.status_code == 200
    with db_session() as s:
        row = s.query(Interjection).filter_by(run_id=r.id, kind="note").one()
        assert "cache" in row.content


def test_stop_queues_interjection(client):
    with db_session() as s:
        r = _add_run(s, ticket="ECD-902", state="in_development")
    rsp = client.post(f"/api/runs/{r.id}/stop")
    assert rsp.status_code == 200
    with db_session() as s:
        s.query(Interjection).filter_by(run_id=r.id, kind="stop").one()


# ─── /api/runs/{id}/block|unblock ─────────────────────────────────


def test_block_creates_operator_block(client):
    with db_session() as s:
        r = _add_run(s, ticket="ECD-A1")
    rsp = client.post(f"/api/runs/{r.id}/block", json={
        "blocker_ticket": "ECD-A0", "reason": "needs decision",
    })
    assert rsp.status_code == 200
    assert "block_id" in rsp.json()
    with db_session() as s:
        b = s.query(Block).filter_by(blocked_run_id=r.id).one()
        assert b.source == "operator"


def test_unblock_clears(client):
    with db_session() as s:
        r = _add_run(s, ticket="ECD-A2")
    record_block(blocked_run_id=r.id, blocker_ticket="ECD-A0", reason="r")
    rsp = client.post(f"/api/runs/{r.id}/unblock")
    assert rsp.status_code == 200
    assert rsp.json()["cleared"] == 1


# ─── /api/stream — publish/subscribe level ────────────────────────


@pytest.mark.asyncio
async def test_publish_reaches_subscriber():
    from ranch.api.events import publish, subscribe

    async def collect_one(gen):
        # Skip the hello frame, then take the next
        first = await gen.__anext__()
        assert "hello" in first
        publish("test_event", {"k": "v"})
        return await gen.__anext__()

    gen = subscribe()
    line = await collect_one(gen)
    assert "test_event" in line
    assert "\"k\": \"v\"" in line
    await gen.aclose()
