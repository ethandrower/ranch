"""Episodic memory: log and query feedback."""
from datetime import datetime, timezone
import re
from .db import db_session
from .models import Feedback, Ticket, FeedbackSource, TicketState
from .transcript import (
    read_transcript,
    get_last_assistant_turn,
    extract_text_and_tools,
    get_recent_file_paths,
)

TICKET_PATTERN = re.compile(r'([A-Z][A-Z0-9]+-\d+)')


def detect_ticket_from_branch(branch: str | None) -> str | None:
    if not branch:
        return None
    m = TICKET_PATTERN.search(branch)
    return m.group(1) if m else None


def get_or_create_ticket(ticket_id: str, agent_name: str | None, branch: str | None) -> int:
    """Returns ticket DB id."""
    with db_session() as db:
        existing = db.query(Ticket).filter_by(ticket_id=ticket_id).first()
        if existing:
            if agent_name and not existing.agent_name:
                existing.agent_name = agent_name
            if branch and not existing.branch_name:
                existing.branch_name = branch
            return existing.id
        ticket = Ticket(
            ticket_id=ticket_id,
            agent_name=agent_name,
            branch_name=branch,
            state=TicketState.IN_DEVELOPMENT.value,
        )
        db.add(ticket)
        db.flush()
        return ticket.id


def log_feedback(
    *,
    user_message: str,
    session_id: str,
    transcript_path: str | None = None,
    cwd: str | None = None,
    agent_name: str | None = None,
    branch: str | None = None,
    source: FeedbackSource = FeedbackSource.USER_CORRECTION,
) -> int | None:
    """Log a feedback row. Returns the row ID, or None if no ticket could be associated."""
    ticket_id = detect_ticket_from_branch(branch)
    if not ticket_id:
        return None

    ticket_db_id = get_or_create_ticket(ticket_id, agent_name, branch)

    prior_text = ""
    prior_tools: list[dict] = []
    files: list[str] = []

    if transcript_path:
        entries = read_transcript(transcript_path)
        last = get_last_assistant_turn(entries)
        if last:
            prior_text, prior_tools = extract_text_and_tools(last)
        files = get_recent_file_paths(entries, n=10)

    with db_session() as db:
        fb = Feedback(
            ticket_db_id=ticket_db_id,
            ticket_id=ticket_id,
            agent_name=agent_name,
            session_id=session_id,
            source=source.value,
            user_message=user_message,
            prior_assistant_text=prior_text or None,
            prior_tool_uses=prior_tools or None,
            file_context=files or None,
            branch_name=branch,
            cwd=cwd,
        )
        db.add(fb)
        db.flush()
        return fb.id


def unprocessed_for_ticket(ticket_id: str) -> list[Feedback]:
    with db_session() as db:
        return (
            db.query(Feedback)
            .filter(
                Feedback.ticket_id == ticket_id,
                Feedback.extracted_to_lesson == 0,
            )
            .order_by(Feedback.timestamp)
            .all()
        )


def mark_processed(feedback_ids: list[int]):
    with db_session() as db:
        db.query(Feedback).filter(Feedback.id.in_(feedback_ids)).update(
            {Feedback.extracted_to_lesson: 1}, synchronize_session=False
        )
