"""FastAPI sidecar — the HTTP face of ranch for the rebuilt console.

Bind: 127.0.0.1:$RANCH_API_PORT (default 8421). Localhost-only by design;
this is not a public API. Electron's main process spawns the sidecar on
boot and proxies renderer requests through it.

Endpoint contract (the React UI consumes these):
    GET  /api/hands                        → [{name, status, attention_count}]
    GET  /api/hands/{name}                 → full HANDS[<name>] view-model
    GET  /api/tickets/{key}                → single-ticket detail bundle
    POST /api/runs/{id}/approve            → wraps `ranch approve <id>`
    POST /api/runs/{id}/reject             → wraps `ranch reject <id>`
    POST /api/runs/{id}/note               → wraps `ranch note <id>`
    POST /api/runs/{id}/stop               → wraps `ranch stop <id>`
    POST /api/runs/{id}/block              → wraps `ranch block <id>`
    POST /api/runs/{id}/unblock            → wraps `ranch unblock <id>`
    GET  /api/stream                       → SSE: dossier/CI/comment events
    GET  /api/health                       → {ok: true, version}
"""
from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

from ..blocks import (
    record_block as _record_block,
    resolve_blocks_for_run,
    resolve_blocks_on_approve,
)
from ..db import db_session
from ..models import HandInitiative, Run
from ..view.hands import build_hand_view
from .events import publish, subscribe


class _NoteBody(BaseModel):
    text: str = ""


class _RejectBody(BaseModel):
    reason: str = ""


class _BlockBody(BaseModel):
    blocker_ticket: str
    reason: str


def create_app() -> FastAPI:
    app = FastAPI(
        title="ranch sidecar",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    # CORS — allow Vite dev server on 5174 (and 5173 if user is dogfooding
    # both). Localhost only, so this is safe.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173", "http://127.0.0.1:5173",
            "http://localhost:5174", "http://127.0.0.1:5174",
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    # ─── Health ────────────────────────────────────────────────────

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "version": "0.1.0"}

    # ─── Hands ─────────────────────────────────────────────────────

    @app.get("/api/hands")
    def list_hands() -> list[dict]:
        """Hand index — one entry per known hand. A "known hand" is any
        hand name with at least one Run or one HandInitiative row.
        Cheap summary; per-hand detail comes from /api/hands/{name}."""
        with db_session() as s:
            run_hands = {row[0] for row in s.execute(select(Run.agent).distinct()).all()}
            init_hands = {row[0] for row in s.execute(select(HandInitiative.hand_name).distinct()).all()}
        names = sorted(run_hands | init_hands)

        result = []
        for name in names:
            view = build_hand_view(name)
            attention = sum(1 for t in view["tickets"] if t.get("attention"))
            result.append({
                "name": name,
                "label": view["label"],
                "status": view["status"],
                "attention_count": attention,
                "ticket_count": len(view["tickets"]),
                "adhoc_count": len(view["adhoc"]),
            })
        return result

    @app.get("/api/hands/{name}")
    def get_hand(name: str) -> dict:
        return build_hand_view(name)

    @app.get("/api/hands/{name}/events")
    def get_hand_events(name: str, limit: int = 50) -> list[dict]:
        """Standalone events fetch — used by the activity popout when it
        wants more history than the embedded events_log carries."""
        from ..events import list_events_for_hand
        return list_events_for_hand(name, limit=limit)

    @app.get("/api/tickets/{key}/activity")
    def get_ticket_activity(key: str, limit: int = 60) -> list[dict]:
        """Live execute activity feed (the agent's reasoning + tool calls) for a
        ticket — what's actually happening during code/verify."""
        from ..events import list_activity_for_ticket
        return list_activity_for_ticket(key, limit=limit)

    @app.get("/api/tickets/{key}/diff")
    def get_ticket_diff(key: str) -> dict:
        """The working diff for a ticket's run, so the operator can review the
        actual code before approving pre_push. Reads the run's worktree."""
        import subprocess
        with db_session() as s:
            run = (
                s.query(Run)
                .filter(Run.ticket == key)
                .order_by(Run.id.desc())
                .first()
            )
            cwd = run.cwd if run else None
            branch = run.branch_name if run else None
        if not cwd:
            return {"ok": False, "reason": "no run/cwd for ticket"}

        def _git(*args: str) -> str:
            try:
                p = subprocess.run(
                    ["git", "-C", cwd, *args],
                    capture_output=True, text=True, timeout=20, check=False,
                )
                return p.stdout
            except Exception:
                return ""

        # Working changes vs HEAD (the code the agent wrote, pre-commit).
        stat = _git("diff", "--stat")
        patch = _git("diff")
        # If nothing unstaged, fall back to the last commit (already committed).
        if not patch.strip():
            stat = _git("diff", "--stat", "HEAD~1..HEAD")
            patch = _git("diff", "HEAD~1..HEAD")
        untracked = [
            u for u in _git("ls-files", "--others", "--exclude-standard").splitlines()
            if u.strip()
        ]
        return {
            "ok": True,
            "branch": branch,
            "cwd": cwd,
            "stat": stat.strip(),
            "patch": patch[:80000],
            "truncated": len(patch) > 80000,
            "untracked": untracked,
        }

    @app.get("/api/hands/{name}/candidates")
    def get_hand_candidates(name: str, project: str | None = None) -> list[dict]:
        """Triage candidates for this hand — Jira tickets routed via
        `ranch-<name>` label that aren't yet in flight. Same query as
        `ranch triage --agent <name>` but JSON-shaped for the UI's
        discovery drawer."""
        from ..jira_backend import resolve_jira_client
        from ..triage import in_flight_ticket_keys_for_agent, triage

        try:
            client_ctx, hand_account = resolve_jira_client()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Jira backend unavailable: {e}")

        in_flight = in_flight_ticket_keys_for_agent(name)
        try:
            with client_ctx as client:
                tickets = client.list_for_hand(
                    name,
                    assignee_account=hand_account or None,
                    project=project,
                )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Jira query failed: {e}")

        ranked = triage(tickets, in_flight)
        return [
            {
                "key": t.key,
                "summary": t.summary,
                "status": t.status,
                "priority": t.priority,
                "labels": t.labels,
                "initiative": t.initiative,
                "score": s.total,
                "has_figma_link": t.has_figma_link,
                "age_days": t.age_days,
            }
            for t, s in ranked
        ]

    class _PickupBody(BaseModel):
        ticket: str
        initiative: str | None = None
        brief: str | None = None  # if absent, we use the Jira summary

    @app.post("/api/hands/{name}/pickup")
    def pickup_ticket(name: str, body: _PickupBody) -> dict:
        """Queue a Jira ticket for this hand to pick up on its next tick.

        Creates a Run row in state='queued'. Does NOT spawn an orchestrator
        — the hand's normal pickup loop will start the propose flow on its
        next poll cycle. Safe to call without a hand running; the row will
        be waiting.
        """
        from ..config import reload_agents
        from ..initiatives import resolve_initiative_for_run
        from ..jira_backend import resolve_jira_client
        from ..models import Run

        agents = reload_agents()
        if name not in agents:
            raise HTTPException(
                status_code=404,
                detail=f"Hand '{name}' not configured. Add it to config.toml.",
            )

        # Pull Jira labels so initiative resolution picks up the
        # ranch-initiative:<key> grouping label if present.
        labels: list[str] = []
        summary_from_jira = ""
        try:
            client_ctx, _ = resolve_jira_client()
            with client_ctx as client:
                jt, _ = client.get_ticket(body.ticket)
                labels = jt.labels
                summary_from_jira = jt.summary
        except Exception:
            pass

        resolved_initiative = resolve_initiative_for_run(
            operator_override=body.initiative,
            ticket_labels=labels,
            hand_name=name,
        )

        brief_text = body.brief or summary_from_jira or f"Work on {body.ticket}."

        with db_session() as s:
            existing = s.query(Run).filter(
                Run.agent == name,
                Run.ticket == body.ticket,
                Run.ended_at.is_(None),
            ).first()
            if existing:
                raise HTTPException(
                    status_code=409,
                    detail=f"{body.ticket} already has an active run (#{existing.id}).",
                )

            run = Run(
                agent=name,
                ticket=body.ticket,
                state="queued",
                cwd=str(agents[name].worktree),
                initial_prompt=brief_text,
                initiative_key=resolved_initiative,
                dispatch_mode="background",
            )
            s.add(run); s.flush()
            run_id = run.id

        publish("ticket_picked_up", {"hand": name, "ticket": body.ticket, "run_id": run_id})
        return {"ok": True, "run_id": run_id, "initiative": resolved_initiative}

    # ─── Tickets ───────────────────────────────────────────────────

    @app.get("/api/tickets/{key}/step-details")
    def get_step_details(key: str) -> dict[str, str]:
        """P6 — per-DONE-step expand-pane content. Walks the ticket's
        dossier history and returns {step_label: details_text}."""
        from ..view.step_details import step_details_for_ticket
        return step_details_for_ticket(key)

    @app.get("/api/tickets/{key}/jira-context")
    def get_jira_context(key: str) -> dict:
        """Read-side Jira context bundle for the side panel — description,
        comments, status, priority, labels, assignee — so the operator
        doesn't have to flip to Jira to read the ticket.

        Routed through the resolved Jira backend; in trinity-mode this
        is `trinity --json jira show <KEY> --comments`, one subprocess
        hop. Returns the raw normalized shape; UI handles truncation
        and expand.
        """
        from ..jira_backend import resolve_jira_client
        try:
            client_ctx, _ = resolve_jira_client()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Jira backend unavailable: {e}")
        try:
            with client_ctx as client:
                if not hasattr(client, "get_ticket_context"):
                    raise HTTPException(
                        status_code=501,
                        detail="Active Jira backend does not support get_ticket_context.",
                    )
                return client.get_ticket_context(key)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Jira fetch failed: {e}")

    @app.get("/api/tickets/{key}")
    def get_ticket(key: str) -> dict:
        """Single-ticket detail bundle. Finds the active Run for this ticket
        (most recent unended) and returns the same view-model shape used in
        /api/hands/{name}.tickets[], plus full dossier history."""
        with db_session() as s:
            run = (
                s.execute(
                    select(Run)
                    .where(Run.ticket == key)
                    .where(Run.ended_at.is_(None))
                    .order_by(Run.started_at.desc())
                    .limit(1)
                )
                .scalar_one_or_none()
            )
            if run is None:
                # Fall back to most recent ended run; UI may be looking at
                # a merged ticket for history.
                run = (
                    s.execute(
                        select(Run)
                        .where(Run.ticket == key)
                        .order_by(Run.started_at.desc())
                        .limit(1)
                    )
                    .scalar_one_or_none()
                )
            if run is None:
                raise HTTPException(status_code=404, detail=f"No run for ticket {key}")

            from ..view.hands import _ticket_view
            view = _ticket_view(s, run)
            view["run_id"] = run.id
            return view

    # ─── Mutations: approve / reject / note / stop ────────────────

    def _queue_interjection(run_id: int, kind: str, content: str) -> dict:
        from ..models import Interjection
        with db_session() as s:
            run = s.query(Run).filter_by(id=run_id).one_or_none()
            if run is None:
                raise HTTPException(status_code=404, detail=f"Run #{run_id} not found")
            s.add(Interjection(run_id=run_id, kind=kind, content=content))
        publish("interjection", {"run_id": run_id, "kind": kind})
        return {"ok": True}

    @app.post("/api/runs/{run_id}/approve")
    def approve(run_id: int, body: _NoteBody = _NoteBody()) -> dict:
        out = _queue_interjection(run_id, "approve", body.text)
        n = resolve_blocks_on_approve(run_id)
        if n:
            publish("blocks_resolved", {"approved_run_id": run_id, "count": n})
        out["unblocked"] = n
        return out

    @app.post("/api/runs/{run_id}/reject")
    def reject(run_id: int, body: _RejectBody) -> dict:
        return _queue_interjection(run_id, "reject", body.reason)

    @app.post("/api/runs/{run_id}/note")
    def note(run_id: int, body: _NoteBody) -> dict:
        return _queue_interjection(run_id, "note", body.text)

    @app.post("/api/runs/{run_id}/stop")
    def stop(run_id: int) -> dict:
        return _queue_interjection(run_id, "stop", "")

    @app.post("/api/runs/{run_id}/kickoff")
    def kickoff(run_id: int) -> dict:
        """Operator kickoff: tell the hand to fire propose on a queued
        triage candidate. The hand's main loop picks up the kickoff
        interjection on its next tick and runs scope + propose."""
        with db_session() as s:
            run = s.query(Run).filter_by(id=run_id).one_or_none()
            if run is None:
                raise HTTPException(status_code=404, detail=f"Run #{run_id} not found")
            if run.state != "queued":
                raise HTTPException(
                    status_code=409,
                    detail=f"Run #{run_id} is in state '{run.state}', not 'queued'.",
                )
        out = _queue_interjection(run_id, "kickoff", "")
        publish("kickoff", {"run_id": run_id, "hand": run.agent, "ticket": run.ticket})
        return out

    # ─── Mutations: block / unblock ───────────────────────────────

    @app.post("/api/runs/{run_id}/block")
    def block(run_id: int, body: _BlockBody) -> dict:
        block = _record_block(
            blocked_run_id=run_id,
            blocker_ticket=body.blocker_ticket,
            reason=body.reason,
            source="operator",
        )
        publish("block_created", {"run_id": run_id, "block_id": block.id})
        return {"ok": True, "block_id": block.id}

    @app.post("/api/runs/{run_id}/unblock")
    def unblock(run_id: int) -> dict:
        n = resolve_blocks_for_run(run_id)
        if n:
            publish("blocks_resolved", {"run_id": run_id, "count": n})
        return {"ok": True, "cleared": n}

    # ─── SSE ───────────────────────────────────────────────────────

    @app.get("/api/stream")
    async def stream() -> EventSourceResponse:
        async def event_generator():
            async for chunk in subscribe():
                yield chunk
        return EventSourceResponse(event_generator())

    return app


app = create_app()
