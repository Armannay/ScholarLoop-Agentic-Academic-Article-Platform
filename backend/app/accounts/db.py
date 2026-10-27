"""Engine and session wiring for the account store.

SQLite in dev/test, Postgres in production - the code never branches on the
dialect. Schema creation happens through :func:`init_db` for dev/tests and through
Alembic (``alembic upgrade head``, revision ``0001``) for real deployments.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.accounts.models import Base

__all__ = ["create_session_factory", "init_db", "make_engine", "session_scope"]


def make_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Build an engine. ``check_same_thread`` is relaxed for SQLite's TestClient use."""
    if database_url.startswith("sqlite"):
        return create_engine(
            database_url, echo=echo, future=True, connect_args={"check_same_thread": False}
        )
    return create_engine(database_url, echo=echo, future=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """A session factory bound to ``engine`` (expire_on_commit=False: API reads after commit)."""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db(engine: Engine) -> None:
    """Create every table if missing. Dev/test convenience; prod runs Alembic.

    The import is local on purpose: this module owns the *account* store's engine,
    and the projects table merely shares the same metadata. Importing it here (rather
    than at module scope) keeps ``app.accounts`` from depending on ``app.projects`` at
    import time while still making ``init_db`` create the full schema a request needs.
    """
    from app.projects.models import ProjectRow  # noqa: F401  (registers the table)

    Base.metadata.create_all(engine)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error, always close."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
