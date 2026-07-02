"""End-to-end loop test — drives ONE ticket from triage through merge
without hitting Jira, the Claude SDK, or git.

The agent's role at each stage is simulated by direct DB writes that
mirror what `record_state` / `record_checkpoint` / `bb pr create` would
produce in production. This lets us verify the wiring across all 10
kanban stages + the side-panel projections without needing live tokens.

For each stage we assert:
1. The Run state in the DB is what the hand would produce next
2. The view-model (consumed by the React UI) projects to the right stage
3. The /api/hands/{name} HTTP endpoint surfaces it correctly

The actual hand `run()` loop isn't started here — we drive the same
transitions it would. The intent is to catch projection regressions, not
to test the asyncio scheduler (which has its own tests).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from ranch.api.app import create_app
from ranch.blocks import record_block, resolve_blocks_on_approve
from ranch.db import db_session
from ranch.events import emit_event
from ranch.models import (
    Block,
    Checkpoint,
    Dossier,
    HandEvent,
    HandInitiative,
    Initiative,
    Interjection,
    ReviewComment,
    Run,
)
from ranch.view.hands import build_hand_view


# ─── Fixtures ───────────────────────────────────────────────────────


HAND = "max"
TICKET = "ECD-E2E"


@pytest.fixture
def hand_world():
    """Seed the world: initiatives + hand mapping. Returns the FastAPI
    TestClient so individual stage assertions can hit the HTTP layer too."""
    with db_session() as s:
        s.add(Initiative(key="ref-mgmt", label="Reference Management"))
        s.add(Initiative(key="misc", label="Misc"))
        s.flush()
        s.add(HandInitiative(
            hand_name=HAND, initiative_key="ref-mgmt",
            is_default=1, sort_order=0,
        ))
        s.add(HandInitiative(
            hand_name=HAND, initiative_key="misc",
            is_default=0, sort_order=1,
        ))
    return TestClient(create_app())


def _stage_of(client, ticket=TICKET) -> str:
    """Helper: pull the current stage of TICKET from the view-model."""
    view = client.get(f"/api/hands/{HAND}").json()
    all_t = view["tickets"] + view["adhoc"]
    target = next(t for t in all_t if t["key"] == ticket)
    return target["stage"]


def _ticket(client, ticket=TICKET) -> dict:
    view = client.get(f"/api/hands/{HAND}").json()
    all_t = view["tickets"] + view["adhoc"]
    return next(t for t in all_t if t["key"] == ticket)


def _dossier_write(s, run_id: int, *, state: str, plan: list, just_did: str,
                   details: str = "", recommended_action: str | None = None,
                   recommendation_reason: str | None = None) -> None:
    payload = {
        "plan": plan, "just_did": just_did, "state": state, "details": details,
    }
    if recommended_action:
        payload["recommended_action"] = recommended_action
        payload["recommendation_reason"] = recommendation_reason
    s.add(Dossier(run_id=run_id, state=state, payload_json=json.dumps(payload)))


# ─── The walkthrough ────────────────────────────────────────────────


def test_drive_one_ticket_through_every_stage(hand_world):
    """The tale of ECD-E2E — operator labels in Jira, hand routes, agent
    builds, operator approves at each gate, PR opens, CI passes, reviewer
    comments, agent responds, PR merges. Every stage projection verified
    against the view-model and the HTTP API."""
    client = hand_world

    # ─── Stage 1: triage (just dispatched) ────────────────────────
    with db_session() as s:
        propose_run = Run(
            agent=HAND, ticket=TICKET, state="queued", cwd="/tmp",
            initial_prompt="Fix typo in README that says 'cite-med' should be 'citemed'.",
            initiative_key="ref-mgmt",
            started_at=datetime.now(timezone.utc),
        )
        s.add(propose_run); s.flush()
        propose_run_id = propose_run.id
    assert _stage_of(client) == "triage"
    emit_event(hand_name=HAND, kind="triage", title=f"Picked {TICKET}",
               severity="info", ticket=TICKET)

    # ─── Stage 2: scope → propose (planning) ──────────────────────
    with db_session() as s:
        run = s.query(Run).filter_by(id=propose_run_id).one()
        run.state = "planning"
        _dossier_write(s, run.id, state="planning",
                       plan=[{"step": "read ticket + repo", "status": "done"}],
                       just_did="read the README and the ticket; drafting fix plan",
                       details="The typo is on line 3 of README.md. Trivial 1-line change.")
    assert _stage_of(client) == "plan"

    # ─── Stage 3: parked at plan_ready (operator decision pending) ─
    # Production semantic: Run finishes (state=completed, ended_at set);
    # the parked-ness is on the DOSSIER not the Run row. The dossier's
    # state="parked" + an unprocessed approve interjection are what
    # _find_approved_parked_propose looks for.
    with db_session() as s:
        run = s.query(Run).filter_by(id=propose_run_id).one()
        run.state = "completed"
        run.ended_at = datetime.now(timezone.utc)
        s.add(Checkpoint(run_id=run.id, kind="plan_ready",
                         summary="Drafted plan: replace 'cite-med' with 'citemed'."))
        _dossier_write(s, run.id, state="parked",
                       plan=[
                           {"step": "read ticket + repo", "status": "done"},
                           {"step": "draft fix plan", "status": "done"},
                       ],
                       just_did="Parked at plan_ready awaiting operator approval.")
    t = _ticket(client)
    assert t["stage"] == "plan"
    assert t["attention"] is True
    assert t["checkpoint"] == "plan_ready"

    # ─── Operator approves plan via HTTP API ──────────────────────
    rsp = client.post(f"/api/runs/{propose_run_id}/approve", json={"text": "ship it"})
    assert rsp.status_code == 200
    with db_session() as s:
        ij = s.query(Interjection).filter_by(run_id=propose_run_id, kind="approve").one()
        # The hand's _find_approved_parked_propose would mark it processed;
        # for the harness, simulate that by walking the same code path.
        from ranch.hand import _find_approved_parked_propose
        approved = _find_approved_parked_propose(HAND)
        assert approved is not None
        assert approved.propose_run.ticket == TICKET

    # ─── Stage 4: execute run created (coding) ────────────────────
    with db_session() as s:
        exec_run = Run(
            agent=HAND, ticket=TICKET, state="in_development", cwd="/tmp",
            initial_prompt="execute brief carried from propose",
            initiative_key="ref-mgmt",
            started_at=datetime.now(timezone.utc),
        )
        s.add(exec_run); s.flush()
        exec_run_id = exec_run.id
        _dossier_write(s, exec_run.id, state="coding",
                       plan=[
                           {"step": "write failing test", "status": "done"},
                           {"step": "fix the typo", "status": "in_progress"},
                       ],
                       just_did="wrote failing test; applying the one-line fix now",
                       details="The test guards against the typo regressing. Diff is 1 line in README.md.")
    assert _stage_of(client) == "code"

    # ─── Stage 5: verify (testing) ────────────────────────────────
    with db_session() as s:
        _dossier_write(s, exec_run_id, state="testing",
                       plan=[
                           {"step": "write failing test", "status": "done"},
                           {"step": "fix the typo", "status": "done"},
                           {"step": "run acceptance", "status": "in_progress"},
                       ],
                       just_did="acceptance running — pytest + grep",
                       details="run_acceptance found 1 check passing already")
    assert _stage_of(client) == "verify"

    # ─── Stage 6: pre_push (parked for operator review of diff) ───
    # Same semantic as plan_ready — Run terminates (state=completed),
    # parked-ness is in the dossier.
    with db_session() as s:
        run = s.query(Run).filter_by(id=exec_run_id).one()
        run.state = "completed"
        run.ended_at = datetime.now(timezone.utc)
        s.add(Checkpoint(run_id=run.id, kind="pre_push",
                         summary="diff +1/-1 in README.md, acceptance 1/1 green"))
        _dossier_write(s, exec_run_id, state="parked",
                       plan=[
                           {"step": "write failing test", "status": "done"},
                           {"step": "fix the typo", "status": "done"},
                           {"step": "run acceptance", "status": "done"},
                       ],
                       just_did="Acceptance 1/1 green. Diff is +1/-1 in README.md.",
                       details="One-character typo fix. No deploy needed — pure docs.",
                       recommended_action="no_deploy",
                       recommendation_reason="docs-only diff, no public URL or migration")
    t = _ticket(client)
    assert t["stage"] == "pre_push"
    assert t["deploy_rec"] == "no-deploy"
    assert t["deploy_reason"].startswith("docs-only")
    assert t["attention"] is True
    assert t["checkpoint"] == "pre_push"

    # ─── Operator approves pre_push ───────────────────────────────
    rsp = client.post(f"/api/runs/{exec_run_id}/approve")
    assert rsp.status_code == 200

    # ─── Stage 7: PR opened (simulating bb pr create succeed) ─────
    # The pre_push approval resumes the run which then pushes + opens
    # the PR. Run.state goes to in_qa (PR open, awaiting reviews) and
    # ended_at clears.
    with db_session() as s:
        run = s.query(Run).filter_by(id=exec_run_id).one()
        run.pr_id = "9999"
        run.pr_platform = "bb"
        run.pr_url = "https://bitbucket.org/citemed/citemed_web/pull-requests/9999"
        run.state = "in_qa"
        run.ended_at = None  # back to active
        # Mark the pre_push checkpoint as decided so the projection knows
        # we're past it, not awaiting it.
        cp = (
            s.query(Checkpoint)
            .filter_by(run_id=exec_run_id, kind="pre_push")
            .order_by(Checkpoint.created_at.desc())
            .first()
        )
        if cp and cp.decision is None:
            cp.decision = "approved"
            cp.decided_at = datetime.now(timezone.utc)
    assert _stage_of(client) == "pr_open"

    # ─── Stage 8: review (CI passed, reviewer comments) ───────────
    with db_session() as s:
        s.add(Checkpoint(run_id=exec_run_id, kind="pre_push",
                         summary="pre_push approved + pushed",
                         decision="approved",
                         decided_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
        s.add(ReviewComment(
            run_id=exec_run_id, platform_comment_id="bb-9999-1",
            author="vinod", body="nit: would also fix the typo in CONTRIBUTING.md",
            file_path="README.md", line_number=3, resolved=0,
        ))
    t = _ticket(client)
    assert t["stage"] == "review"
    assert t["decide_kind"] == "respond_to_review"

    # Resolve the comment (operator OR agent acted on the nit)
    with db_session() as s:
        rc = s.query(ReviewComment).filter_by(run_id=exec_run_id).one()
        rc.resolved = 1

    # ─── Stage 9: merge ──────────────────────────────────────────
    with db_session() as s:
        run = s.query(Run).filter_by(id=exec_run_id).one()
        run.state = "merged"
        run.ended_at = datetime.now(timezone.utc)
    assert _stage_of(client) == "merge"

    # ─── Side-panel projections: step-details join works ─────────
    rsp = client.get(f"/api/tickets/{TICKET}/step-details")
    assert rsp.status_code == 200
    details = rsp.json()
    # Every done step should have a key (even if value is empty).
    assert "fix the typo" in details
    assert "run acceptance" in details
    # The "fix the typo" step transitioned to done at the verify dossier
    # write — pin that the join captured the right content.
    assert "1 check passing" in details["fix the typo"]

    # ─── Events log carries the timeline ─────────────────────────
    events = client.get(f"/api/hands/{HAND}/events").json()
    assert len(events) >= 1
    assert any(TICKET in (e.get("title") or "") for e in events)


def test_blocked_run_skipped_until_unblock_cascade(hand_world):
    """E2E: dependent run sits in `plan` with ⛔; approving the blocker
    cascades the unblock and the dependent's queued approve fires."""
    client = hand_world

    with db_session() as s:
        # Blocker — operator will approve its plan
        blocker = Run(
            agent=HAND, ticket="ECD-B1", state="completed", cwd="/tmp",
            initial_prompt="blocker work",
            initiative_key="ref-mgmt",
            started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            ended_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        s.add(blocker); s.flush()
        s.add(Checkpoint(run_id=blocker.id, kind="plan_ready", summary="ready"))
        _dossier_write(s, blocker.id, state="parked",
                       plan=[{"step": "draft plan", "status": "done"}],
                       just_did="parked at plan_ready")

        # Dependent — already has an approve queued, but is blocked
        dep = Run(
            agent=HAND, ticket="ECD-D1", state="completed", cwd="/tmp",
            initial_prompt="dependent work",
            initiative_key="ref-mgmt",
            started_at=datetime.now(timezone.utc) - timedelta(minutes=8),
            ended_at=datetime.now(timezone.utc) - timedelta(seconds=30),
        )
        s.add(dep); s.flush()
        s.add(Checkpoint(run_id=dep.id, kind="plan_ready", summary="ready"))
        _dossier_write(s, dep.id, state="parked",
                       plan=[{"step": "draft plan", "status": "done"}],
                       just_did="parked at plan_ready")
        s.add(Interjection(run_id=dep.id, kind="approve", content=""))

        blocker_id, dep_id = blocker.id, dep.id

    record_block(blocked_run_id=dep_id, blocker_ticket="ECD-B1",
                 reason="dep on blocker's plan call")

    # Dependent shows blocked
    t = _ticket(client, "ECD-D1")
    assert t["blocked_by"] == "ECD-B1"

    # _find_approved_parked_propose should refuse to fire the dependent
    from ranch.hand import _find_approved_parked_propose
    assert _find_approved_parked_propose(HAND) is None  # blocked

    # Operator approves blocker via HTTP
    rsp = client.post(f"/api/runs/{blocker_id}/approve")
    assert rsp.status_code == 200
    assert rsp.json()["unblocked"] == 1

    # Block resolved on the dependent
    t = _ticket(client, "ECD-D1")
    assert "blocked_by" not in t

    # Dependent's existing approve interjection now fires
    approved = _find_approved_parked_propose(HAND)
    assert approved is not None
    assert approved.propose_run.ticket == "ECD-D1"
