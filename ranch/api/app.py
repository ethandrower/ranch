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

    # ─── Tickets ───────────────────────────────────────────────────

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
