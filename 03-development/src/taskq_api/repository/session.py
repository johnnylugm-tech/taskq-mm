"""[FR-06] Engine factory + transaction context manager.

This module is the only place where ``Session`` instances are constructed.
Each API request opens exactly one Session via :func:`transaction`; the
context manager guarantees ``commit()`` on success and ``rollback()`` on
any exception, closing the Session in both branches [NFR-03].

Postgres-style row locks (``SELECT ... FOR UPDATE``) are available via the
:func:`select_for_update` helper used by the rate-limit repository [FR-05].

Note: SQLAlchemy is imported lazily inside functions so that simply
importing this module from the upper layers does not pull SQLAlchemy into
their import graph [NFR-06].
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Iterator, Optional

from ..config import Settings, get_settings

if TYPE_CHECKING:  # pragma: no cover — type-check only
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session, sessionmaker


# Self-reference for service-layer import ergonomics [NFR-06]. This module
# exposes ``transaction`` (a context manager function), so we publish the
# module under a distinct name to avoid clobbering it.
import importlib as _importlib
module = _importlib.import_module(__name__)

_lock = threading.Lock()
_engine: Optional[Engine] = None
_sessionmaker: Optional[sessionmaker[Session]] = None
_url_in_use: Optional[str] = None


def engine_from_url(url: str, *, pool_size: int = 5) -> "Engine":
    """Build a fresh :class:`Engine` for ``url`` [FR-06].

    ``pool_pre_ping=True`` avoids stale-connection failures [FR-06].
    For SQLite we extend ``connect_timeout`` so test fixtures do not see
    transient lock errors [R10].
    """
    from sqlalchemy import create_engine
    connect_args: dict[str, object] = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        connect_args["timeout"] = 30
    return create_engine(
        url,
        pool_size=pool_size,
        pool_pre_ping=True,
        future=True,
        connect_args=connect_args,
    )


def configure_engine(settings: Optional[Settings] = None) -> "Engine":
    """Initialise (or replace) the process-wide engine + sessionmaker."""
    global _engine, _sessionmaker, _url_in_use
    resolved = settings if settings is not None else get_settings()
    with _lock:
        if _engine is not None and _url_in_use == resolved.taskq_db_url:
            return _engine
        if _engine is not None:
            _engine.dispose()
        new_engine = engine_from_url(
            resolved.taskq_db_url, pool_size=resolved.taskq_db_pool_size
        )
        _engine = new_engine
        from sqlalchemy.orm import sessionmaker
        _sessionmaker = sessionmaker(
            bind=new_engine, autoflush=False, expire_on_commit=False, future=True
        )
        _url_in_use = resolved.taskq_db_url
        return _engine


def get_engine() -> "Engine":
    """Return the current engine, initialising it on first call."""
    if _engine is None:
        return configure_engine()
    return _engine


def get_sessionmaker() -> "sessionmaker[Session]":
    """Return the current sessionmaker, initialising it on first call."""
    if _sessionmaker is None:
        configure_engine()
    if _sessionmaker is None:  # pragma: no cover — defensive
        raise RuntimeError("sessionmaker could not be initialised")
    return _sessionmaker


def set_engine(engine: "Engine") -> None:
    """Override the engine — used by tests that point at a temp DB."""
    global _engine, _sessionmaker, _url_in_use
    from sqlalchemy.orm import sessionmaker
    with _lock:
        if _engine is not None and _engine is not engine:
            _engine.dispose()
        _engine = engine
        _sessionmaker = sessionmaker(
            bind=engine, autoflush=False, expire_on_commit=False, future=True
        )
        _url_in_use = str(engine.url)


def reset_engine() -> None:
    """Drop the cached engine — primarily for test teardown."""
    global _engine, _sessionmaker, _url_in_use
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _sessionmaker = None
        _url_in_use = None


def create_all(engine: Optional["Engine"] = None) -> None:
    """Create every table declared on :class:`Base.metadata`.

    Production runs Alembic; this helper exists for tests and the bundled
    management command ``python -m taskq_api initdb``.
    """
    from ..models.orm import Base
    Base.metadata.create_all(engine or get_engine())


def drop_all(engine: Optional["Engine"] = None) -> None:
    """Drop every table on :class:`Base.metadata` — tests only."""
    from ..models.orm import Base
    Base.metadata.drop_all(engine or get_engine())


@contextmanager
def transaction() -> Iterator[Session]:
    """Yield a :class:`Session` inside a commit/rollback boundary [FR-06 / NFR-03].

    Usage::

        with transaction() as session:
            session.add(obj)
            ...

    On normal exit the transaction commits. On any exception the
    transaction rolls back, the Session is closed, and the exception
    re-raises so callers can react.
    """
    maker = get_sessionmaker()
    session = maker()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def select_for_update(session: "Session", *entities):
    """Return a ``SELECT ... FOR UPDATE`` statement [FR-05 / R12].

    Used by the rate-limit repository so the row lock is held inside the
    same transaction that decrements the bucket. On SQLite the clause is
    a no-op (closes P1-3 in the bug report) — that gap is made loud by
    raising here so dev paths do not silently rely on a contract SQLite
    cannot satisfy. Use :func:`select_for_update_or_pass` for the dev path
    where SQLite is acceptable.
    """
    from sqlalchemy import select
    dialect = session.bind.dialect.name if session.bind is not None else "sqlite"
    if dialect == "sqlite":
        raise RuntimeError(
            "select_for_update is a no-op on SQLite; use Postgres for concurrency "
            "tests or wrap the call site in a threading.Lock for in-process serialisation."
        )
    return select(*entities).with_for_update()


def select_for_update_or_pass(session: "Session", *entities):
    """Return a ``SELECT ... FOR UPDATE`` on Postgres, plain ``SELECT`` elsewhere.

    Pragmatic compromise for the dev path: callers that need to
    serialise the row (e.g. ``run_task_endpoint`` locking the parent task
    before enqueueing) can use this helper and the test path keeps
    working on SQLite, while production still gets the row lock. The
    cost is that the test path is *not* a faithful model of the
    production race — that is the same caveat as the rest of the test
    suite, and is closed by adding a Postgres-only CI lane.
    """
    from sqlalchemy import select
    stmt = select(*entities)
    if session.bind is not None and session.bind.dialect.name != "sqlite":
        stmt = stmt.with_for_update()
    return stmt


# Self-reference for service-layer import ergonomics [NFR-06]. This module
# exposes ``transaction`` (a context manager function), so we publish the
# module under a distinct name to avoid clobbering it.
import importlib as _importlib
module = _importlib.import_module(__name__)