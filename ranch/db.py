"""SQLAlchemy engine and session factory."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager
from .config import DATABASE_URL

engine = create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def db_session() -> Session:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db():
    from .models import Base
    Base.metadata.create_all(engine)
    _migrate()


def _migrate():
    """Apply column additions that `create_all` can't retrofit on existing tables.

    No Alembic — this is a lightweight idempotent upgrade path for SQLite.
    Each entry: (table, column, DDL column definition).
    """
    from sqlalchemy import inspect, text

    additions = [
        ("interjections", "processed_at", "DATETIME"),
        ("runs", "free", "INTEGER DEFAULT 0"),
        ("runs", "auto_approve", "INTEGER DEFAULT 0"),
        ("runs", "dispatch_mode", "VARCHAR DEFAULT 'foreground'"),
        ("runs", "pid", "INTEGER"),
        ("runs", "log_path", "VARCHAR"),
        ("runs", "branch_name", "VARCHAR"),
        ("runs", "pr_id", "VARCHAR"),
        ("runs", "pr_platform", "VARCHAR"),
        ("runs", "pr_url", "VARCHAR"),
    ]
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    for table, col, ddl in additions:
        if table not in existing_tables:
            continue
        cols = {c["name"] for c in insp.get_columns(table)}
        if col in cols:
            continue
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
