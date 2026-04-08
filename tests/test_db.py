from ranch.db import init_db, db_session
from ranch.models import Ticket, Feedback


def test_init_db():
    init_db()
    with db_session() as db:
        assert db.query(Ticket).count() >= 0
