"""SQLAlchemy models. SQLite now, PostgreSQL later — same schema."""
from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, DateTime, Text, JSON, ForeignKey, Index,
)
from sqlalchemy.orm import declarative_base, relationship
import enum

Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc)


# ─── Enums (stored as strings) ────────────────────────────────

class FeedbackSource(str, enum.Enum):
    USER_CORRECTION   = "user_correction"     # user message in CC
    INLINE_COMMENT    = "inline_comment"      # PR review (later)
    PR_COMMENT        = "pr_comment"          # PR review (later)
    BUILD_FAILURE     = "build_failure"       # CI (later)
    APPROVAL_REJECT   = "approval_reject"     # checkpoint rejection (Phase 2)
    APPROVAL_COMMENT  = "approval_comment"    # checkpoint comment (Phase 2)


class TicketState(str, enum.Enum):
    QUEUED          = "queued"
    PLANNING        = "planning"
    NEEDS_APPROVAL  = "needs_approval"
    IN_DEVELOPMENT  = "in_development"
    IN_QA           = "in_qa"
    FINAL_APPROVAL  = "final_approval"
    DONE            = "done"
    ERROR           = "error"


class LessonCategory(str, enum.Enum):
    CODE_STYLE       = "code_style"
    ARCHITECTURE     = "architecture"
    TESTING          = "testing"
    TOOLING          = "tooling"
    REVIEWER_PREF    = "reviewer_preference"
    ERROR_HANDLING   = "error_handling"
    SECURITY         = "security"
    PERFORMANCE      = "performance"
    DJANGO_SPECIFIC  = "django_specific"
    REPO_CONVENTION  = "repo_convention"
    OTHER            = "other"


# ─── Models ───────────────────────────────────────────────────

class Ticket(Base):
    """A unit of work. In Phase 1, this gets created lazily when feedback is captured."""
    __tablename__ = "tickets"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    ticket_id    = Column(String, unique=True, index=True)   # e.g. "PROJ-123"
    title        = Column(String, default="")
    agent_name   = Column(String, nullable=True, index=True) # max | jeffy | arnold
    state        = Column(String, default=TicketState.IN_DEVELOPMENT.value, index=True)
    branch_name  = Column(String, nullable=True)
    created_at   = Column(DateTime, default=utcnow)
    completed_at = Column(DateTime, nullable=True)
    reflected_at = Column(DateTime, nullable=True)  # last time reflection ran for this ticket

    feedback = relationship("Feedback", back_populates="ticket", lazy="dynamic")


class Feedback(Base):
    """Episodic memory. Every correction, comment, or signal received during a ticket."""
    __tablename__ = "feedback"

    id                   = Column(Integer, primary_key=True, autoincrement=True)
    ticket_db_id         = Column(Integer, ForeignKey("tickets.id"), index=True)
    ticket_id            = Column(String, index=True)        # denormalized
    agent_name           = Column(String, index=True)        # max | jeffy | arnold
    session_id           = Column(String, index=True)        # CC session ID
    timestamp            = Column(DateTime, default=utcnow, index=True)

    source               = Column(String, index=True)        # FeedbackSource
    user_message         = Column(Text)                      # what the user said
    prior_assistant_text = Column(Text, nullable=True)       # the assistant turn it was responding to
    prior_tool_uses      = Column(JSON, nullable=True)       # list of tool calls in that prior turn
    file_context         = Column(JSON, nullable=True)       # which files were touched recently
    branch_name          = Column(String, nullable=True)
    cwd                  = Column(String, nullable=True)

    extracted_to_lesson  = Column(Integer, default=0)        # bool: has reflection processed this?

    ticket = relationship("Ticket", back_populates="feedback")


Index("ix_feedback_ticket_unprocessed", Feedback.ticket_id, Feedback.extracted_to_lesson)


class Lesson(Base):
    """Semantic memory. A distilled, reusable learning."""
    __tablename__ = "lessons"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    created_at       = Column(DateTime, default=utcnow)
    updated_at       = Column(DateTime, default=utcnow)

    content          = Column(Text)                      # the lesson statement
    category         = Column(String, index=True)        # LessonCategory
    confidence       = Column(Integer, default=1)         # 1-5
    times_reinforced = Column(Integer, default=1)

    source_ticket_ids   = Column(JSON, default=list)    # ["PROJ-123", "PROJ-456"]
    source_feedback_ids = Column(JSON, default=list)    # [12, 34]

    applies_to_files = Column(JSON, nullable=True)      # ["**/serializers.py"]
    applies_to_tags  = Column(JSON, nullable=True)      # ["django", "api"]
    applies_always   = Column(Integer, default=0)        # bool

    is_active         = Column(Integer, default=1)        # bool
    deprecated_reason = Column(Text, nullable=True)


class ReflectionRun(Base):
    """Audit log of every reflection invocation."""
    __tablename__ = "reflection_runs"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    timestamp          = Column(DateTime, default=utcnow)
    ticket_id          = Column(String, index=True)
    agent_name         = Column(String, nullable=True)
    feedback_count     = Column(Integer)
    lessons_created    = Column(Integer, default=0)
    lessons_reinforced = Column(Integer, default=0)
    duration_seconds   = Column(Integer, default=0)
    cost_cents         = Column(Integer, default=0)
    summary            = Column(Text, nullable=True)
    error              = Column(Text, nullable=True)
