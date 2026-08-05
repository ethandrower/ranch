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


# ─── Phase 2 models ───────────────────────────────────────────

class Run(Base):
    """A checkpointed orchestrated run via ranch run / ranch dispatch."""
    __tablename__ = "runs"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    agent               = Column(String, index=True)        # max | jeffy | arnold
    ticket              = Column(String, nullable=True, index=True)
    sdk_session_id      = Column(String, nullable=True)     # for resume
    state               = Column(String, default="queued", index=True)
    state_before_pause  = Column(String, nullable=True)     # restored on approve/reject
    started_at          = Column(DateTime, default=utcnow)
    ended_at            = Column(DateTime, nullable=True)
    exit_reason         = Column(String, nullable=True)     # completed | stopped | error | needs_approval
    cwd                 = Column(String)
    initial_prompt      = Column(Text)
    free                = Column(Integer, default=0)        # bool: --free flag
    auto_approve        = Column(Integer, default=0)        # bool: --auto-approve flag
    dispatch_mode       = Column(String, default="foreground")  # foreground | background
    pid                 = Column(Integer, nullable=True)    # PID of the detached orchestrator process
    log_path            = Column(String, nullable=True)     # file path for background logs
    branch_name         = Column(String, nullable=True)     # captured at end-of-run from git
    pr_id               = Column(String, nullable=True)     # discovered via bb/gh pr list --head <branch>
    pr_platform         = Column(String, nullable=True)     # "bb" | "gh"
    pr_url              = Column(String, nullable=True)
    last_pr_check_at    = Column(DateTime, nullable=True)   # H20: last time the hand polled this PR for review comments
    # H9 — per-hand staging deploy state (operator-driven, not auto-fire)
    deploy_url          = Column(String, nullable=True)     # public URL after deploy succeeded
    deployed_at         = Column(DateTime, nullable=True)   # when the deploy + health check completed
    # Console rebuild P0 — which initiative this run's ticket belongs to.
    # Denormalized for fast board-per-initiative filtering; source of truth
    # is the Jira label, captured at triage time. Nullable for legacy runs.
    initiative_key      = Column(String, ForeignKey("initiatives.key"), nullable=True, index=True)
    # Operator-kickoff flow: triage queues Runs in state='queued' with the
    # hand's viability score; the operator picks which to kick off via the
    # UI. Score is shown on the triage card as ranking signal.
    triage_score        = Column(Integer, nullable=True)
    triage_summary      = Column(Text, nullable=True)  # Jira summary, copied at queue time

    checkpoints    = relationship("Checkpoint",    back_populates="run", lazy="dynamic")
    interjections  = relationship("Interjection",  back_populates="run", lazy="dynamic")
    review_comments = relationship("ReviewComment", back_populates="run", lazy="dynamic")


class Checkpoint(Base):
    """A pause point where the model announces it needs human review."""
    __tablename__ = "checkpoints"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    run_id          = Column(Integer, ForeignKey("runs.id"), index=True)
    kind            = Column(String)                    # plan_ready | tests_green | pre_push | custom
    summary         = Column(Text)
    payload_json    = Column(Text, nullable=True)       # diff stats, file list, etc
    created_at      = Column(DateTime, default=utcnow)
    decision        = Column(String, nullable=True)     # approved | rejected
    decision_note   = Column(Text, nullable=True)
    decided_at      = Column(DateTime, nullable=True)

    run = relationship("Run", back_populates="checkpoints")


class ReviewComment(Base):
    """A PR review comment fetched from bb/gh and fed back to the agent for triage."""
    __tablename__ = "review_comments"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    run_id              = Column(Integer, ForeignKey("runs.id"), index=True)
    platform_comment_id = Column(String, index=True)          # unique per platform (bb|gh)
    author              = Column(String, nullable=True)
    file_path           = Column(String, nullable=True)
    line_number         = Column(Integer, nullable=True)
    body                = Column(Text)
    created_at_remote   = Column(DateTime, nullable=True)
    fetched_at          = Column(DateTime, default=utcnow)
    resolved            = Column(Integer, default=0)          # bool
    resolved_commit_sha = Column(String, nullable=True)

    run = relationship("Run", back_populates="review_comments")


Index(
    "ix_review_comments_unique_platform_id",
    ReviewComment.run_id, ReviewComment.platform_comment_id,
    unique=True,
)


class Interjection(Base):
    """A human command sent mid-run. Written by either the foreground stdin loop
    or the out-of-process CLI (`ranch approve/reject/note/stop <run_id>`).
    The orchestrator's DB poll loop consumes rows where processed_at IS NULL."""
    __tablename__ = "interjections"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    run_id        = Column(Integer, ForeignKey("runs.id"), index=True)
    kind          = Column(String)                    # note | stop | approve | reject | redirect
    content       = Column(Text)
    created_at    = Column(DateTime, default=utcnow)
    processed_at  = Column(DateTime, nullable=True, index=True)

    run = relationship("Run", back_populates="interjections")


class PRCIStatus(Base):
    """H20 P2 — append-only audit trail of CI status flips per Run/commit.

    The hand polls each PR's CI on a cadence and writes a new row each
    time the normalized status differs from the previous row. Lets us
    answer "when did this build go red?" without scrolling BB/GH UI.
    """
    __tablename__ = "pr_ci_status"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    run_id      = Column(Integer, ForeignKey("runs.id"), index=True)
    pr_id       = Column(String, index=True)
    commit_sha  = Column(String, nullable=True)
    status      = Column(String)  # queued | running | passed | failed | stopped | unknown
    fetched_at  = Column(DateTime, default=utcnow, index=True)


class Dossier(Base):
    """Agent self-report — structured snapshot of where the run is right now.

    One row per `record_state` call. Latest-wins for the "current state" query
    (`ORDER BY created_at DESC LIMIT 1`); older rows are retained so the timeline
    view (Phase E3) can replay how the run evolved. See ROADMAP Phase H2.
    """
    __tablename__ = "dossiers"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    run_id       = Column(Integer, ForeignKey("runs.id"), index=True)
    state        = Column(String, index=True)         # denormalized for "show parked runs" filtering
    payload_json = Column(Text)                       # full RecordStateInput as JSON
    created_at   = Column(DateTime, default=utcnow, index=True)


# ─── Initiatives + blocks (Phase H console-rebuild P0) ────────────────


class Initiative(Base):
    """A coarse-grained scope a hand watches (e.g. "ref-mgmt", "scrapers").

    Tickets carry an `initiative_key` and the UI's board-per-initiative model
    filters by it. Source of truth: Jira label `ranch-initiative:<key>` on the
    ticket; CLI override allowed for operator overrides at triage time.
    """
    __tablename__ = "initiatives"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    key         = Column(String, unique=True, index=True)   # "ref-mgmt"
    label       = Column(String)                            # "Reference Management"
    description = Column(Text, nullable=True)
    created_at  = Column(DateTime, default=utcnow)


class HandInitiative(Base):
    """Which initiatives a given hand watches, with a designated default.

    Composite-PK via (hand_name, initiative_key). One row per (hand, init)
    pair. Exactly one row per hand should have is_default=1; enforced at
    write time, not by the schema.
    """
    __tablename__ = "hand_initiatives"

    hand_name       = Column(String, primary_key=True, index=True)
    initiative_key  = Column(String, ForeignKey("initiatives.key"), primary_key=True)
    is_default      = Column(Integer, default=0)   # bool
    sort_order      = Column(Integer, default=0)   # display order in the scope-bar


class Block(Base):
    """A "ticket B is blocked by ticket A's decision" relationship.

    Lives as its own table because it's a graph edge, not a self-report —
    multiple agents/operators may emit blocks against the same run, and we
    need cheap "show all blocks resolved by this checkpoint" queries.

    `resolved_at` non-null = the block has lifted (e.g. blocker's
    plan_ready was approved). Hands skip runs that have any unresolved
    blocks pointing at them.
    """
    __tablename__ = "blocks"

    id                          = Column(Integer, primary_key=True, autoincrement=True)
    blocked_run_id              = Column(Integer, ForeignKey("runs.id"), index=True)
    blocker_run_id              = Column(Integer, ForeignKey("runs.id"), nullable=True, index=True)
    blocker_ticket              = Column(String, nullable=True)   # captured even if blocker Run row doesn't exist yet
    reason                      = Column(Text)
    created_at                  = Column(DateTime, default=utcnow, index=True)
    resolved_at                 = Column(DateTime, nullable=True, index=True)
    resolved_by_checkpoint_id   = Column(Integer, ForeignKey("checkpoints.id"), nullable=True)
    # Source: "agent" (via record_block MCP tool) or "operator" (via `ranch block` CLI).
    source                      = Column(String, default="agent")


class HandEvent(Base):
    """A single timeline entry per hand — what changed and when.

    Populated by the hand orchestrator on state transitions, CI flips,
    review-comment fetches, triage decisions, deploys, and block
    create/resolve. Consumed by the console's Activity popout +
    per-hand events log.

    Append-only — never updated, never deleted. Old rows can be pruned
    by age, but we don't write that yet.
    """
    __tablename__ = "hand_events"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    hand_name   = Column(String, index=True)
    ticket      = Column(String, nullable=True)
    kind        = Column(String, index=True)   # e.g. "state_transition", "ci_flip", "review_comment", "block_created"
    severity    = Column(String, default="info")  # good | bad | warn | info
    icon        = Column(String, default="·")
    title       = Column(String)
    detail      = Column(Text, nullable=True)
    created_at  = Column(DateTime, default=utcnow, index=True)


class Verdict(Base):
    """A browser-verification verdict — one row per `ranch verify` session.

    The verifier session (fresh context, Playwright MCP only) judges each
    acceptance criterion by ACTING in the real UI, then files exactly one
    verdict via the record_verdict MCP tool. `payload_json` holds the full
    VerdictInput (per-criterion pass/fail + evidence + screenshot names);
    `artifacts_dir` is where those screenshots live on disk.
    """
    __tablename__ = "verdicts"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    run_id        = Column(Integer, ForeignKey("runs.id"), nullable=True, index=True)
    ticket        = Column(String, nullable=True, index=True)
    target_url    = Column(String)
    overall_pass  = Column(Integer, default=0)     # bool
    payload_json  = Column(Text)                   # full VerdictInput as JSON
    artifacts_dir = Column(String, nullable=True)  # screenshots location
    created_at    = Column(DateTime, default=utcnow, index=True)
