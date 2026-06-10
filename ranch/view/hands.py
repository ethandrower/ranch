"""Read-side view-model — projects DB state onto the shape the new console
expects (see prototypes/per-hand-tabs.html).

Pure function: takes a hand name, returns a dict matching the prototype's
HANDS[<name>] shape. The HTTP sidecar in P2 serves this verbatim; the
React UI consumes it as-is.

We deliberately do NOT pull from the agent runtime here (no in-process
locks, no SDK imports). The view-model is a snapshot from the DB; if the
UI wants live updates it subscribes to the SSE stream (P5) which fires
whenever a write lands.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import db_session
from ..models import (
    Block,
    Dossier,
    HandInitiative,
    Initiative,
    ReviewComment,
    Run,
)


# ─── Stage mapping ─────────────────────────────────────────────────
#
# The 10-stage kanban in the prototype is the canonical pipeline. Each Run's
# (state, exit_reason, last_checkpoint_kind, pr_id, pr_state) tuple maps onto
# exactly one stage. Keep this table flat and reviewable rather than scattered
# through conditionals.
#
# Stages: triage | scope | plan | code | verify | pre_push | deploy | pr_open
#         | review | merge

STAGES = (
    "triage", "scope", "plan", "code", "verify", "pre_push",
    "deploy", "pr_open", "review", "merge",
)


def _project_stage(run: Run, latest_cp_kind: Optional[str], latest_dossier_state: Optional[str]) -> str:
    """Pick the kanban column this run belongs in.

    Precedence (most specific first):
    - Run.state == "merged"           → merge
    - Run has pr_id + last cp resolved → review (we're past pre_push and PR exists)
    - Run has pr_id, awaiting reviews  → pr_open
    - Run.deployed_at set              → deploy (parked or just-deployed pre-PR)
    - Last checkpoint kind == pre_push → pre_push
    - Dossier state == "testing"      → verify
    - Dossier state == "coding"       → code
    - Last checkpoint kind == plan_ready → plan
    - Dossier state == "planning"     → plan
    - Run.state == "queued"           → triage
    - Anything else                    → scope (researching, judging)
    """
    if run.state == "merged":
        return "merge"
    if run.pr_id:
        # Differentiate pr_open vs review by whether reviews have started.
        # Cheap proxy: any inbound review_comment rows = review; else pr_open.
        # Caller injects this via the view builder; here we default to review
        # if last_cp_kind is "pre_push" (means we've pushed and PR exists).
        return "review" if latest_cp_kind == "pre_push" else "pr_open"
    if run.deployed_at is not None:
        return "deploy"
    if latest_cp_kind == "pre_push":
        return "pre_push"
    if latest_dossier_state == "testing":
        return "verify"
    if latest_dossier_state == "coding":
        return "code"
    if latest_cp_kind == "plan_ready" or latest_dossier_state == "planning":
        return "plan"
    if run.state == "queued":
        return "triage"
    return "scope"


# ─── View-model assembly ────────────────────────────────────────────


@dataclass
class _LatestDossier:
    state: Optional[str]
    payload: dict[str, Any]


def _latest_dossier(session: Session, run_id: int) -> Optional[_LatestDossier]:
    row = (
        session.execute(
            select(Dossier)
            .where(Dossier.run_id == run_id)
            .order_by(Dossier.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if row is None:
        return None
    try:
        payload = json.loads(row.payload_json) if row.payload_json else {}
    except json.JSONDecodeError:
        payload = {}
    return _LatestDossier(state=row.state, payload=payload)


def _latest_checkpoint_kind(session: Session, run_id: int) -> Optional[str]:
    """Return the kind of the most recent checkpoint for a run, regardless of
    whether it was approved / rejected / still pending."""
    from ..models import Checkpoint
    row = (
        session.execute(
            select(Checkpoint.kind)
            .where(Checkpoint.run_id == run_id)
            .order_by(Checkpoint.created_at.desc())
            .limit(1)
        )
        .first()
    )
    return row[0] if row else None


def _pending_checkpoint(session: Session, run_id: int) -> Optional[str]:
    """Return the kind of the most recent checkpoint that has NOT been decided."""
    from ..models import Checkpoint
    row = (
        session.execute(
            select(Checkpoint.kind)
            .where(Checkpoint.run_id == run_id)
            .where(Checkpoint.decision.is_(None))
            .order_by(Checkpoint.created_at.desc())
            .limit(1)
        )
        .first()
    )
    return row[0] if row else None


def _block_for(session: Session, run_id: int) -> Optional[Block]:
    """Return the most recent unresolved block against a run, or None."""
    return (
        session.execute(
            select(Block)
            .where(Block.blocked_run_id == run_id)
            .where(Block.resolved_at.is_(None))
            .order_by(Block.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def _unresolved_review_comment_count(session: Session, run_id: int) -> int:
    from sqlalchemy import func
    return session.execute(
        select(func.count(ReviewComment.id))
        .where(ReviewComment.run_id == run_id)
        .where(ReviewComment.resolved == 0)
    ).scalar() or 0


def _ticket_view(session: Session, run: Run) -> dict[str, Any]:
    """Project a single Run + its latest Dossier into the prototype's
    ticket-object shape: key, initiative, epic, stage, summary, goal,
    done[], now, attention, checkpoint, blocked_by, blocked_reason, etc.

    Fields the prototype shows but we don't yet emit are simply omitted —
    the React UI tolerates absent keys (see prototype's renderPanel which
    checks each section before rendering).
    """
    dossier = _latest_dossier(session, run.id)
    latest_cp = _latest_checkpoint_kind(session, run.id)
    pending_cp = _pending_checkpoint(session, run.id)
    block = _block_for(session, run.id)

    plan = (dossier.payload.get("plan") or []) if dossier else []
    done_steps = [step.get("step", "") for step in plan if step.get("status") == "done"]

    just_did = dossier.payload.get("just_did") if dossier else None
    blocker_text = dossier.payload.get("blocker") if dossier else None

    stage = _project_stage(run, latest_cp, dossier.state if dossier else None)

    attention = bool(pending_cp) or bool(block) or run.state == "needs_approval"

    out: dict[str, Any] = {
        "key": run.ticket or f"(run-{run.id})",
        "initiative": run.initiative_key,
        "epic": None,  # not yet captured — Jira parent link is P0+; defer
        "stage": stage,
        "summary": (run.initial_prompt or "").splitlines()[0][:120] if run.initial_prompt else "",
        "goal": run.initial_prompt or "",
        "done": done_steps,
    }

    if just_did:
        out["now"] = {
            "line": just_did,
            "meta": f"state={dossier.state}" if dossier else "",
        }

    if attention:
        out["attention"] = True
    if pending_cp:
        out["checkpoint"] = pending_cp
    if block:
        out["blocked_by"] = block.blocker_ticket
        out["blocked_reason"] = block.reason

    rec_action = dossier.payload.get("recommended_action") if dossier else None
    if rec_action:
        out["deploy_rec"] = {"deploy": "deploy", "no_deploy": "no-deploy", "needs_review": "needs-review"}.get(rec_action, rec_action)
        out["deploy_reason"] = dossier.payload.get("recommendation_reason")

    if run.pr_id:
        out["pr_id"] = run.pr_id
        if _unresolved_review_comment_count(session, run.id) > 0:
            out["decide_kind"] = "respond_to_review"

    return out


def _initiative_meta(session: Session, hand_name: str) -> tuple[list[str], Optional[str], dict[str, str]]:
    """Return (initiative_keys_in_sort_order, default_key, key_to_label_map)
    for a given hand."""
    rows = (
        session.execute(
            select(HandInitiative.initiative_key, HandInitiative.is_default,
                   HandInitiative.sort_order, Initiative.label)
            .join(Initiative, Initiative.key == HandInitiative.initiative_key)
            .where(HandInitiative.hand_name == hand_name)
            .order_by(HandInitiative.sort_order)
        )
        .all()
    )
    keys = [r[0] for r in rows]
    default = next((r[0] for r in rows if r[1]), keys[0] if keys else None)
    labels = {r[0]: r[3] for r in rows}
    return keys, default, labels


def build_hand_view(hand_name: str) -> dict[str, Any]:
    """Build the prototype's HANDS[<hand_name>] view-object from the DB.

    Returns a dict ready for JSON serialization. Status, initiatives,
    tickets, adhoc, events_log shapes match the prototype contract.
    """
    with db_session() as session:
        runs = (
            session.execute(
                select(Run)
                .where(Run.agent == hand_name)
                .where(Run.ended_at.is_(None))
                .order_by(Run.started_at.desc())
            )
            .scalars()
            .all()
        )

        tickets: list[dict[str, Any]] = []
        adhoc: list[dict[str, Any]] = []
        for run in runs:
            view = _ticket_view(session, run)
            if run.ticket:
                tickets.append(view)
            else:
                view["adhoc"] = True
                adhoc.append(view)

        initiatives, default_initiative, labels = _initiative_meta(session, hand_name)

        # Hand status: "running" if any non-parked active run exists, else "idle".
        active = any(r.state not in ("parked", "queued", "needs_approval") for r in runs)
        status = "running" if active else "idle"

        # P5 — populate events_log from the hand_events table.
        from ..events import list_events_for_hand
        events_log = list_events_for_hand(hand_name, limit=20)

        return {
            "label": hand_name,
            "status": status,
            "initiatives": initiatives,
            "default_initiative": default_initiative,
            "initiative_labels": labels,
            "tickets": tickets,
            "adhoc": adhoc,
            "events_log": events_log,
            # Routines (jira_triage / pr_comments / etc cadence indicators)
            # need a HandHeartbeat write-side — deferred. Emit empty so the
            # UI keeps the activity-popout's status section happy.
            "routines": {},
        }
