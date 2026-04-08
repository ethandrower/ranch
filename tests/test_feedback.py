from ranch.feedback import detect_ticket_from_branch, log_feedback
from ranch.db import init_db, db_session
from ranch.models import Feedback


def setup_function():
    init_db()


def test_detect_ticket():
    assert detect_ticket_from_branch("feature/PROJ-123-foo") == "PROJ-123"
    assert detect_ticket_from_branch("main") is None
    assert detect_ticket_from_branch(None) is None
    assert detect_ticket_from_branch("feature/AB-99-thing") == "AB-99"


def test_log_feedback_no_branch():
    result = log_feedback(
        user_message="test",
        session_id="abc",
        branch=None,
    )
    assert result is None


def test_log_feedback_with_ticket():
    fid = log_feedback(
        user_message="use snake_case",
        session_id="test-session-1",
        branch="feature/TEST-1-validation",
    )
    assert fid is not None
    with db_session() as db:
        fb = db.query(Feedback).filter_by(id=fid).one()
        assert fb.ticket_id == "TEST-1"
        assert fb.user_message == "use snake_case"


def test_log_feedback_creates_ticket():
    from ranch.models import Ticket
    fid = log_feedback(
        user_message="always use factory_boy for fixtures",
        session_id="test-session-2",
        branch="feature/TEST-2-fixtures",
        agent_name="max",
    )
    assert fid is not None
    with db_session() as db:
        fb = db.query(Feedback).filter_by(id=fid).one()
        ticket = db.query(Ticket).filter_by(ticket_id="TEST-2").one()
        assert ticket.agent_name == "max"
        assert fb.agent_name == "max"
