"""Single point of write for hand_events.

Use `emit_event(hand_name, kind, title, ...)` anywhere a meaningful
hand-level change happens (state transition, CI flip, block, deploy).
The sidecar's SSE stream also publishes a real-time event on each emit
so connected UIs see the timeline update without a refetch.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select

from .db import db_session
from .models import HandEvent


def emit_event(
    *,
    hand_name: str,
    kind: str,
    title: str,
    detail: Optional[str] = None,
    severity: str = "info",
    icon: Optional[str] = None,
    ticket: Optional[str] = None,
) -> HandEvent:
    """Append a HandEvent row and (best-effort) publish to the SSE bus."""
    if icon is None:
        icon = _default_icon(kind, severity)
    with db_session() as session:
        row = HandEvent(
            hand_name=hand_name,
            ticket=ticket,
            kind=kind,
            severity=severity,
            icon=icon,
            title=title,
            detail=detail,
        )
        session.add(row)
        session.flush()
        session.refresh(row)
        session.expunge(row)

    # Best-effort publish — import lazily so non-sidecar contexts
    # (CLI, tests) don't pay for it.
    try:
        from .api.events import publish
        publish("hand_event", {
            "hand_name": hand_name,
            "kind": kind,
            "title": title,
            "detail": detail,
            "severity": severity,
            "icon": icon,
            "ticket": ticket,
        })
    except Exception:
        # Sidecar not running — log to DB is what matters.
        pass

    return row


def _default_icon(kind: str, severity: str) -> str:
    """A reasonable icon per kind. Override per-call when needed."""
    by_kind = {
        "state_transition": "↪",
        "ci_flip": "⚡",
        "review_comment": "💬",
        "block_created": "⛔",
        "block_resolved": "↻",
        "deploy": "↓",
        "merge": "✓",
        "approval": "✓",
        "rejection": "✗",
        "triage": "⟳",
    }
    if kind in by_kind:
        return by_kind[kind]
    if severity == "good":
        return "✓"
    if severity == "bad":
        return "✗"
    return "·"


def list_events_for_hand(hand_name: str, limit: int = 50) -> list[dict]:
    """Newest-first list of recent events for a hand, projected into the
    prototype's events_log shape (icon/severity/title/detail/ago).
    """
    with db_session() as session:
        rows = (
            session.execute(
                select(HandEvent)
                .where(HandEvent.hand_name == hand_name)
                .order_by(HandEvent.created_at.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        for r in rows:
            session.expunge(r)
    return [
        {
            "icon": r.icon,
            "severity": r.severity,
            "title": r.title,
            "detail": r.detail,
            "ago": _humanize_ago(r.created_at),
            "ticket": r.ticket,
        }
        for r in rows
    ]


def list_activity_for_ticket(ticket: str, limit: int = 60) -> list[dict]:
    """Newest-first execute activity (the agent's reasoning + tool calls) for a
    ticket — powers the console's live 'what is the agent doing' feed."""
    with db_session() as session:
        rows = (
            session.execute(
                select(HandEvent)
                .where(HandEvent.ticket == ticket)
                .where(HandEvent.kind == "activity")
                .order_by(HandEvent.created_at.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        for r in rows:
            session.expunge(r)
    return [
        {"icon": r.icon, "title": r.title, "detail": r.detail, "ago": _humanize_ago(r.created_at)}
        for r in rows
    ]


def _humanize_ago(ts: Optional[datetime]) -> str:
    if ts is None:
        return "?"
    # Normalize to UTC-aware
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - ts
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"


_TICKET_RE = re.compile(r"\b([A-Z]+-\d+)\b")


def extract_ticket(text: Optional[str]) -> Optional[str]:
    """Best-effort ticket key extraction for free-form titles."""
    if not text:
        return None
    m = _TICKET_RE.search(text)
    return m.group(1) if m else None
