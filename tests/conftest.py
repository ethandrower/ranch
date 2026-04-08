"""Test configuration — isolates each test from the production DB."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point ranch at a fresh temp DB for every test, then tear it down."""
    import ranch.db as _db

    test_engine = create_engine(
        f"sqlite:///{tmp_path}/test_ranch.db", echo=False, future=True
    )
    test_session_local = sessionmaker(bind=test_engine, expire_on_commit=False)

    monkeypatch.setattr(_db, "engine", test_engine)
    monkeypatch.setattr(_db, "SessionLocal", test_session_local)

    from ranch.db import init_db
    init_db()
    yield
    test_engine.dispose()
