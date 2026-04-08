"""Semantic memory: create, reinforce, and query lessons."""
from datetime import datetime, timezone
from .db import db_session
from .models import Lesson


def create_lesson(
    *,
    content: str,
    category: str,
    source_ticket_ids: list[str],
    source_feedback_ids: list[int],
    applies_to_files: list[str] | None = None,
    applies_to_tags: list[str] | None = None,
    applies_always: bool = False,
    confidence: int = 1,
) -> int:
    with db_session() as db:
        lesson = Lesson(
            content=content,
            category=category,
            confidence=max(1, min(5, confidence)),
            times_reinforced=1,
            source_ticket_ids=source_ticket_ids,
            source_feedback_ids=source_feedback_ids,
            applies_to_files=applies_to_files,
            applies_to_tags=applies_to_tags,
            applies_always=1 if applies_always else 0,
        )
        db.add(lesson)
        db.flush()
        return lesson.id


def reinforce_lesson(lesson_id: int, ticket_id: str, feedback_ids: list[int]):
    with db_session() as db:
        lesson = db.query(Lesson).filter_by(id=lesson_id).one()
        lesson.times_reinforced += 1
        if lesson.confidence < 5 and lesson.times_reinforced >= lesson.confidence * 2:
            lesson.confidence += 1
        existing_tickets = list(lesson.source_ticket_ids or [])
        if ticket_id not in existing_tickets:
            existing_tickets.append(ticket_id)
        lesson.source_ticket_ids = existing_tickets
        existing_feedback = list(lesson.source_feedback_ids or [])
        existing_feedback.extend(feedback_ids)
        lesson.source_feedback_ids = existing_feedback
        lesson.updated_at = datetime.now(timezone.utc)


def list_active(category: str | None = None) -> list[Lesson]:
    with db_session() as db:
        q = db.query(Lesson).filter(Lesson.is_active == 1)
        if category:
            q = q.filter(Lesson.category == category)
        return q.order_by(Lesson.confidence.desc()).all()
