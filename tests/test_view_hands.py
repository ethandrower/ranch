"""Tests for ranch.view.hands.build_hand_view — the read-side projection
that feeds the new console UI.

The shape returned here is the contract the React UI consumes via the HTTP
sidecar (P2). Pin it tightly: missing keys = silent UI bugs.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from ranch.db import db_session
from ranch.models import (
    Block,
    Checkpoint,
    Dossier,
    HandInitiative,
    Initiative,
    ReviewComment,
    Run,
)
from ranch.view.hands import build_hand_view


# ─── Helpers ───────────────────────────────────────────────────────


def _seed_initiative(session, key: str, label: str, hand: str, *, is_default: int = 0, order: int = 0) -> None:
    session.add(Initiative(key=key, label=label))
    session.flush()
    session.add(HandInitiative(hand_name=hand, initiative_key=key, is_default=is_default, sort_order=order))


def _add_run(session, **kwargs) -> Run:
    defaults = dict(agent="max", state="queued", cwd="/tmp", initial_prompt="do thing")
    defaults.update(kwargs)
    r = Run(**defaults)
    session.add(r)
    session.flush()
    return r


def _add_dossier(session, run_id: int, *, state: str, plan: list[dict] | None = None,
                 just_did: str = "did stuff", blocker: str | None = None,
                 recommended_action: str | None = None, recommendation_reason: str | None = None) -> None:
    payload = {
        "plan": plan or [],
        "just_did": just_did,
        "state": state,
    }
    if blocker:
        payload["blocker"] = blocker
    if recommended_action:
        payload["recommended_action"] = recommended_action
        payload["recommendation_reason"] = recommendation_reason
    session.add(Dossier(run_id=run_id, state=state, payload_json=json.dumps(payload)))


# ─── Empty world ───────────────────────────────────────────────────


def test_empty_world_returns_shell():
    view = build_hand_view("nobody")
    assert view["label"] == "nobody"
    assert view["status"] == "idle"
    assert view["tickets"] == []
    assert view["adhoc"] == []
    assert view["initiatives"] == []
    assert view["default_initiative"] is None
    # P5 fields emitted as empty shells, never missing
    assert view["events_log"] == []
    assert view["routines"] == {}


# ─── Initiative meta ───────────────────────────────────────────────


def test_initiatives_are_returned_in_sort_order():
    with db_session() as s:
        _seed_initiative(s, "misc", "Misc", "max", order=2)
        _seed_initiative(s, "ref-mgmt", "Reference Management", "max", is_default=1, order=0)
        _seed_initiative(s, "scrapers", "Scrapers", "max", order=1)
    view = build_hand_view("max")
    assert view["initiatives"] == ["ref-mgmt", "scrapers", "misc"]
    assert view["default_initiative"] == "ref-mgmt"
    assert view["initiative_labels"]["ref-mgmt"] == "Reference Management"


def test_default_falls_back_to_first_when_no_flag():
    with db_session() as s:
        _seed_initiative(s, "ref-mgmt", "Reference Management", "max", order=0)
        _seed_initiative(s, "misc", "Misc", "max", order=1)
    view = build_hand_view("max")
    assert view["default_initiative"] == "ref-mgmt"


# ─── Stage projection ──────────────────────────────────────────────


def test_run_with_no_dossier_in_queued_is_triage():
    with db_session() as s:
        _add_run(s, ticket="ECD-1", state="queued")
    view = build_hand_view("max")
    assert view["tickets"][0]["stage"] == "triage"


def test_run_with_coding_dossier_is_code():
    with db_session() as s:
        r = _add_run(s, ticket="ECD-2", state="in_development")
        _add_dossier(s, r.id, state="coding", plan=[{"step": "wire up", "status": "in_progress"}])
    view = build_hand_view("max")
    assert view["tickets"][0]["stage"] == "code"


def test_run_with_pre_push_checkpoint_is_pre_push():
    with db_session() as s:
        r = _add_run(s, ticket="ECD-3", state="needs_approval")
        s.add(Checkpoint(run_id=r.id, kind="pre_push", summary="ready"))
    view = build_hand_view("max")
    tk = view["tickets"][0]
    assert tk["stage"] == "pre_push"
    # Pending pre_push → attention + checkpoint marker
    assert tk["attention"] is True
    assert tk["checkpoint"] == "pre_push"


def test_run_with_pr_id_no_reviews_is_pr_open():
    with db_session() as s:
        r = _add_run(s, ticket="ECD-4", state="in_qa", pr_id="1234", pr_platform="bb")
        # No pre_push checkpoint = "just opened a PR", not "PR has been reviewed"
    view = build_hand_view("max")
    assert view["tickets"][0]["stage"] == "pr_open"
    assert view["tickets"][0]["pr_id"] == "1234"


def test_run_with_pr_and_pre_push_history_is_pr_open_until_comments():
    """Phase B-era projection: pre_push + pr_id alone is just-pushed (pr_open).
    Stage advances to review only when an unresolved review comment arrives."""
    with db_session() as s:
        r = _add_run(s, ticket="ECD-5", state="in_qa", pr_id="1620", pr_platform="bb")
        s.add(Checkpoint(run_id=r.id, kind="pre_push", summary="ready", decision="approved"))
    view = build_hand_view("max")
    assert view["tickets"][0]["stage"] == "pr_open"

    # Comment arrives → flips to review
    with db_session() as s:
        s.add(ReviewComment(run_id=r.id, platform_comment_id="c-2",
                            body="why?", resolved=0))
    view = build_hand_view("max")
    assert view["tickets"][0]["stage"] == "review"


def test_run_with_pr_and_unresolved_review_comment_signals_respond_kind():
    with db_session() as s:
        r = _add_run(s, ticket="ECD-6", state="in_qa", pr_id="1620")
        s.add(Checkpoint(run_id=r.id, kind="pre_push", summary="ready", decision="approved"))
        s.add(ReviewComment(run_id=r.id, platform_comment_id="c-1", body="why this?", resolved=0))
    view = build_hand_view("max")
    tk = view["tickets"][0]
    assert tk["decide_kind"] == "respond_to_review"


def test_merged_run_is_merge_stage():
    with db_session() as s:
        _add_run(s, ticket="ECD-7", state="merged")
    view = build_hand_view("max")
    assert view["tickets"][0]["stage"] == "merge"


# ─── Done-list projection ──────────────────────────────────────────


def test_done_steps_pulled_from_dossier_plan():
    with db_session() as s:
        r = _add_run(s, ticket="ECD-10", state="in_development")
        _add_dossier(s, r.id, state="coding", plan=[
            {"step": "read ticket", "status": "done"},
            {"step": "write failing test", "status": "done"},
            {"step": "implement", "status": "in_progress"},
        ])
    view = build_hand_view("max")
    assert view["tickets"][0]["done"] == ["read ticket", "write failing test"]


# ─── Deploy recommendation projection ──────────────────────────────


def test_deploy_recommendation_maps_to_prototype_label():
    with db_session() as s:
        r = _add_run(s, ticket="ECD-11", state="parked")
        s.add(Checkpoint(run_id=r.id, kind="pre_push", summary="ready"))
        _add_dossier(s, r.id, state="parked",
                     recommended_action="no_deploy",
                     recommendation_reason="contained logic, no public URL")
    view = build_hand_view("max")
    tk = view["tickets"][0]
    assert tk["deploy_rec"] == "no-deploy"
    assert tk["deploy_reason"] == "contained logic, no public URL"


# ─── Block projection ──────────────────────────────────────────────


def test_unresolved_block_appears_on_ticket():
    with db_session() as s:
        r = _add_run(s, ticket="ECD-20", state="queued")
        s.add(Block(
            blocked_run_id=r.id, blocker_ticket="ECD-19",
            reason="depends on plan-call in ECD-19", source="agent",
        ))
    view = build_hand_view("max")
    tk = view["tickets"][0]
    assert tk["blocked_by"] == "ECD-19"
    assert "depends on plan-call" in tk["blocked_reason"]
    assert tk["attention"] is True  # blocks count as attention


def test_resolved_block_is_not_shown():
    with db_session() as s:
        r = _add_run(s, ticket="ECD-21", state="queued")
        s.add(Block(
            blocked_run_id=r.id, blocker_ticket="ECD-19",
            reason="old block", resolved_at=datetime.now(timezone.utc),
        ))
    view = build_hand_view("max")
    tk = view["tickets"][0]
    assert "blocked_by" not in tk


# ─── Adhoc vs ticket separation ────────────────────────────────────


def test_run_without_ticket_lands_in_adhoc():
    with db_session() as s:
        _add_run(s, ticket=None, state="in_development", initial_prompt="investigate queue depth")
    view = build_hand_view("max")
    assert view["tickets"] == []
    assert len(view["adhoc"]) == 1
    assert view["adhoc"][0]["adhoc"] is True


# ─── Hand status ───────────────────────────────────────────────────


def test_hand_with_only_parked_runs_is_idle():
    with db_session() as s:
        _add_run(s, ticket="ECD-30", state="parked")
        _add_run(s, ticket="ECD-31", state="queued")
    view = build_hand_view("max")
    assert view["status"] == "idle"


def test_hand_with_in_development_run_is_running():
    with db_session() as s:
        _add_run(s, ticket="ECD-40", state="in_development")
    view = build_hand_view("max")
    assert view["status"] == "running"


# ─── Ended runs filtered out ──────────────────────────────────────


def test_recently_ended_runs_stay_visible():
    """Phase B: ended runs within the 14-day window remain on the board so
    parked propose runs awaiting operator review and just-merged tickets
    stay visible. Older runs drop off."""
    with db_session() as s:
        _add_run(s, ticket="ECD-50", state="completed",
                 ended_at=datetime.now(timezone.utc) - timedelta(days=1))
    view = build_hand_view("max")
    assert len(view["tickets"]) == 1
    assert view["tickets"][0]["key"] == "ECD-50"


def test_very_old_ended_runs_drop_off():
    with db_session() as s:
        _add_run(s, ticket="ECD-51", state="completed",
                 ended_at=datetime.now(timezone.utc) - timedelta(days=30))
    view = build_hand_view("max")
    assert view["tickets"] == []
