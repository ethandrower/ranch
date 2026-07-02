"""Per-step details — projection from dossier history.

The agent emits a single `details` string per `record_state` call. The UI
wants per-DONE-step expand content. We join read-side by walking the
dossier timeline forward and capturing the first dossier where each step
flipped pending/in_progress → done. That dossier's `details` becomes the
expand-pane content for that step.
"""
from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import select

from ..db import db_session
from ..models import Dossier, Run


def step_details_for_run(run_id: int) -> dict[str, str]:
    """Return a {step_label: details_text} map for a run's done steps.

    Empty string for steps whose transition-point dossier had no details.
    Steps never marked done are omitted entirely.
    """
    out: dict[str, str] = {}
    seen_done: set[str] = set()

    with db_session() as session:
        rows = (
            session.execute(
                select(Dossier)
                .where(Dossier.run_id == run_id)
                .order_by(Dossier.created_at.asc())
            )
            .scalars()
            .all()
        )

        for row in rows:
            try:
                payload = json.loads(row.payload_json) if row.payload_json else {}
            except json.JSONDecodeError:
                continue
            plan = payload.get("plan") or []
            details = payload.get("details") or ""
            for step in plan:
                label = (step.get("step") or "").strip()
                status = step.get("status")
                if not label or status != "done":
                    continue
                if label in seen_done:
                    continue
                # First time we see this step in done state — that
                # dossier's details is the step's narrative.
                out[label] = details
                seen_done.add(label)

    return out


def step_details_for_ticket(ticket: str) -> dict[str, str]:
    """Convenience: resolve the most recent (or active) run for a ticket,
    then return its step details."""
    with db_session() as session:
        run = (
            session.execute(
                select(Run)
                .where(Run.ticket == ticket)
                .order_by(Run.ended_at.is_(None).desc(), Run.started_at.desc())
                .limit(1)
            )
            .scalar_one_or_none()
        )
        if run is None:
            return {}
        run_id = run.id
    return step_details_for_run(run_id)
