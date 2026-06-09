"""Tests for ranch/ci_loop.py — H20 Phase 2.

Covers status normalization, polling + flip detection, persistence to
PRCIStatus, the candidate finder, and the dossier-emission helper.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ranch.ci_loop import (
    CIPollResult,
    PRCICandidate,
    _normalize_bb_status,
    _normalize_gh_status,
    emit_ci_flip_dossier,
    poll_ci_for_run,
    runs_pending_ci_check,
)
from ranch.db import db_session, init_db
from ranch.models import Dossier, PRCIStatus, Run


def _make_run(
    *, agent: str = "max", state: str = "completed",
    pr_id: str | None = "42", pr_platform: str = "bb",
    cwd: str = "/tmp",
) -> int:
    init_db()
    with db_session() as db:
        run = Run(
            agent=agent, ticket="ECD-1", cwd=cwd, initial_prompt="x",
            state=state, pr_id=pr_id, pr_platform=pr_platform,
            ended_at=datetime.now(timezone.utc),
        )
        db.add(run); db.flush()
        return run.id


def _add_ci_status(run_id: int, status: str, sha: str | None = None):
    with db_session() as db:
        db.add(PRCIStatus(
            run_id=run_id, pr_id="42", commit_sha=sha,
            status=status, fetched_at=datetime.now(timezone.utc),
        ))


# ─── Normalization ───────────────────────────────────────────────


def test_normalize_bb_in_progress():
    raw = {"state": {"name": "IN_PROGRESS"}, "target": {"commit": {"hash": "abc"}}}
    assert _normalize_bb_status(raw) == ("running", "abc")


def test_normalize_bb_completed_successful():
    raw = {"state": {"name": "COMPLETED", "result": {"name": "SUCCESSFUL"}},
           "target": {"commit": {"hash": "def"}}}
    assert _normalize_bb_status(raw) == ("passed", "def")


def test_normalize_bb_completed_failed():
    raw = {"state": {"name": "COMPLETED", "result": {"name": "FAILED"}},
           "target": {"commit": {"hash": "xyz"}}}
    assert _normalize_bb_status(raw) == ("failed", "xyz")


def test_normalize_bb_unknown_state():
    raw = {"state": {"name": "MYSTERY_STATE"}}
    status, sha = _normalize_bb_status(raw)
    assert status == "unknown"


def test_normalize_gh_queued():
    assert _normalize_gh_status({"status": "queued", "headSha": "abc"}) == ("queued", "abc")


def test_normalize_gh_success():
    assert _normalize_gh_status({"status": "completed", "conclusion": "success", "headSha": "abc"}) == ("passed", "abc")


def test_normalize_gh_failure():
    assert _normalize_gh_status({"status": "completed", "conclusion": "failure", "headSha": "abc"}) == ("failed", "abc")


def test_normalize_gh_cancelled_is_failed():
    assert _normalize_gh_status({"status": "completed", "conclusion": "cancelled"})[0] == "failed"


# ─── poll_ci_for_run ─────────────────────────────────────────────


def test_poll_ci_missing_run():
    init_db()
    r = poll_ci_for_run(99_999)
    assert r.ok is False
    assert "not found" in r.reason


def test_poll_ci_no_pr_id():
    rid = _make_run(pr_id=None)
    r = poll_ci_for_run(rid)
    assert r.ok is False
    assert "no pr_id" in r.reason


def test_poll_ci_returns_status_without_persisting_when_unchanged():
    """If the backend reports the same status as last seen, no new row."""
    rid = _make_run()
    _add_ci_status(rid, "running")

    with patch("ranch.ci_loop._bb_pipelines_for_pr", return_value=("running", "sha1")):
        r = poll_ci_for_run(rid)

    assert r.ok and r.status == "running"
    assert r.flipped is False
    with db_session() as db:
        rows = db.query(PRCIStatus).filter_by(run_id=rid).all()
    assert len(rows) == 1  # no new row


def test_poll_ci_persists_and_flips_when_status_changes():
    rid = _make_run()
    _add_ci_status(rid, "running")

    with patch("ranch.ci_loop._bb_pipelines_for_pr", return_value=("passed", "sha2")):
        r = poll_ci_for_run(rid)

    assert r.flipped is True
    assert r.status == "passed"
    assert r.previous_status == "running"
    with db_session() as db:
        rows = db.query(PRCIStatus).filter_by(run_id=rid).order_by(PRCIStatus.id).all()
    assert [row.status for row in rows] == ["running", "passed"]


def test_poll_ci_first_observation_is_not_a_flip():
    """When previous status is None (first poll), don't mark flipped."""
    rid = _make_run()
    with patch("ranch.ci_loop._bb_pipelines_for_pr", return_value=("passed", "sha")):
        r = poll_ci_for_run(rid)
    assert r.status == "passed"
    assert r.flipped is False
    # But we DO persist (status changed from None to passed)
    with db_session() as db:
        assert db.query(PRCIStatus).filter_by(run_id=rid).count() == 1


def test_poll_ci_unknown_platform_errors():
    rid = _make_run(pr_platform="wat")
    r = poll_ci_for_run(rid)
    assert r.ok is False
    assert "platform" in r.reason


def test_poll_ci_backend_unusable_returns_soft_none():
    """If `bb pipeline list` errors out, we return ok=True with status=None
    plus a reason — the hand should NOT treat this as a flip."""
    rid = _make_run()
    with patch("ranch.ci_loop._bb_pipelines_for_pr", return_value=None):
        r = poll_ci_for_run(rid)
    assert r.ok is True
    assert r.status is None
    assert r.flipped is False


def test_poll_ci_gh_platform_routes_to_gh_backend():
    rid = _make_run(pr_platform="gh")
    with patch("ranch.ci_loop._gh_runs_for_pr", return_value=("passed", None)) as fake:
        poll_ci_for_run(rid)
    assert fake.called


# ─── runs_pending_ci_check ───────────────────────────────────────


def test_pending_ci_excludes_non_terminal_runs():
    rid = _make_run(state="planning", pr_id="42")
    assert runs_pending_ci_check("max") == []


def test_pending_ci_excludes_runs_without_pr_id():
    rid = _make_run(pr_id=None, state="completed")
    assert runs_pending_ci_check("max") == []


def test_pending_ci_includes_terminal_with_pr_id():
    rid = _make_run()
    out = runs_pending_ci_check("max")
    assert len(out) == 1
    assert out[0].run_id == rid


def test_pending_ci_scoped_by_agent():
    rid_j = _make_run(agent="jeffy", pr_id="50")
    rid_m = _make_run(agent="max", pr_id="51")
    out = runs_pending_ci_check("max")
    assert [c.run_id for c in out] == [rid_m]


def test_pending_ci_no_agent_filter_returns_all():
    rid_a = _make_run(agent="max", pr_id="60")
    rid_b = _make_run(agent="jeffy", pr_id="61")
    ids = {c.run_id for c in runs_pending_ci_check()}
    assert ids == {rid_a, rid_b}


# ─── emit_ci_flip_dossier ────────────────────────────────────────


def test_emit_ci_flip_writes_dossier_row_on_flipped():
    rid = _make_run()
    result = CIPollResult(
        ok=True, pr_id="42", commit_sha="abc",
        status="passed", flipped=True, previous_status="running",
    )
    emit_ci_flip_dossier(rid, result)
    with db_session() as db:
        d = db.query(Dossier).filter_by(run_id=rid).order_by(Dossier.id.desc()).first()
    assert d is not None
    payload = json.loads(d.payload_json)
    assert "CI passed on PR #42" in payload["just_did"]
    assert "running → passed" in payload["details"]


def test_emit_ci_flip_skips_when_not_flipped():
    rid = _make_run()
    result = CIPollResult(
        ok=True, pr_id="42", status="running", flipped=False,
    )
    emit_ci_flip_dossier(rid, result)
    with db_session() as db:
        assert db.query(Dossier).filter_by(run_id=rid).count() == 0


def test_emit_ci_flip_handles_failed_status():
    rid = _make_run()
    result = CIPollResult(
        ok=True, pr_id="42", commit_sha="bad",
        status="failed", flipped=True, previous_status="running",
    )
    emit_ci_flip_dossier(rid, result)
    with db_session() as db:
        d = db.query(Dossier).filter_by(run_id=rid).order_by(Dossier.id.desc()).first()
    payload = json.loads(d.payload_json)
    assert "CI failed on PR #42" in payload["just_did"]
