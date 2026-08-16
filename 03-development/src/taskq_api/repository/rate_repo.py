"""Per-token token bucket persistence [FR-05 / NFR-01 / R12].

Bucket state is stored in the database so the limit is consistent across
worker processes. The read-modify-write sequence runs inside a single
transaction with a row-level lock (``SELECT ... FOR UPDATE``) to avoid
race over-admission.

The cold-start path uses ``INSERT ... ON CONFLICT DO NOTHING`` (closes
P1-8: two concurrent first-time consumers used to race between
``session.get`` and ``session.add``; the upsert collapses the race into
a single round-trip).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..models.orm import APIKey, RateBucket
from .session import select_for_update, select_for_update_or_pass

# Self-reference for service-layer import ergonomics [NFR-06].
import importlib as _importlib
rate_repo = _importlib.import_module(__name__)


def init_rate_bucket(session: Session, key: APIKey, *, capacity: int) -> RateBucket:
    """Seed a bucket at full capacity if none exists yet [FR-05 / P1-8].

    Uses ``INSERT ... ON CONFLICT DO NOTHING`` (Postgres / SQLite ≥ 3.24)
    so two concurrent first-time consumers cannot race between
    ``session.get`` and ``session.add``. The follow-up select picks up
    the row that was inserted by this statement (or already existed).
    """
    dialect = session.bind.dialect.name if session.bind is not None else "sqlite"
    now = datetime.now(tz=timezone.utc)
    if dialect == "postgresql":
        stmt = pg_insert(RateBucket).values(
            key_id=key.id, tokens=capacity, updated_at=now
        ).on_conflict_do_nothing(index_elements=[RateBucket.key_id])
    else:
        stmt = sqlite_insert(RateBucket).values(
            key_id=key.id, tokens=capacity, updated_at=now
        ).on_conflict_do_nothing(index_elements=[RateBucket.key_id])
    session.execute(stmt)
    bucket = session.execute(
        select(RateBucket).where(RateBucket.key_id == key.id)
    ).scalar_one()
    return bucket


def take_token(
    session: Session,
    key: APIKey,
    *,
    capacity: int,
    refill_per_sec: float,
    now: datetime | None = None,
) -> Tuple[bool, float]:
    """Try to consume a single token for ``key``.

    Returns ``(allowed, retry_after)``. ``retry_after`` is the seconds the
    caller must wait before another request would succeed; for non-limited
    responses it is ``0.0``.

    The cold-start path uses the upsert in :func:`init_rate_bucket`
    rather than a TOCTOU get-then-add (closes P1-8 + P1-9).
    """
    now = now or datetime.now(tz=timezone.utc)
    # ``select_for_update_or_pass`` keeps the test path usable on SQLite
    # (the lock is a no-op there) while still giving the real row lock
    # on Postgres. Closing the test/prod parity gap needs a Postgres-only
    # CI lane.
    bucket = session.execute(
        select_for_update_or_pass(session, RateBucket).where(RateBucket.key_id == key.id)
    ).scalar_one_or_none()
    if bucket is None:
        # Cold start — same transaction (so the lock is still held).
        init_rate_bucket(session, key, capacity=capacity)
        bucket = session.execute(
            select_for_update_or_pass(session, RateBucket).where(RateBucket.key_id == key.id)
        ).scalar_one()
    # SQLite drops tzinfo on round-trip — coerce to UTC-naive so we can
    # subtract without TypeError [FR-05].
    bucket_ts = bucket.updated_at
    if bucket_ts.tzinfo is None:
        bucket_ts = bucket_ts.replace(tzinfo=timezone.utc)
    elapsed = (now - bucket_ts).total_seconds()
    if elapsed > 0:
        bucket.tokens = min(float(capacity), bucket.tokens + elapsed * refill_per_sec)
        bucket.updated_at = now.replace(tzinfo=None)
    if bucket.tokens >= 1.0:
        bucket.tokens -= 1.0
        session.flush()
        return True, 0.0
    deficit = 1.0 - bucket.tokens
    retry_after = deficit / refill_per_sec if refill_per_sec > 0 else 1.0
    session.flush()
    return False, retry_after


def _reset_bucket_for_key(session: Session, key_id: int, *, tokens: float) -> None:
    """Set ``rate_buckets.tokens`` for the given key — used by revocation.

    The bucket row is created on demand so revocation of a key that has
    never made an authenticated request still records the empty state.
    This is the architectural fix for P1-10: a revoked key can no longer
    drain residual tokens before the next request sees the new
    ``revoked_at`` flag.
    """
    dialect = session.bind.dialect.name if session.bind is not None else "sqlite"
    now = datetime.now(tz=timezone.utc)
    if dialect == "postgresql":
        stmt = pg_insert(RateBucket).values(
            key_id=key_id, tokens=tokens, updated_at=now
        ).on_conflict_do_update(
            index_elements=[RateBucket.key_id],
            set_={"tokens": tokens, "updated_at": now},
        )
    else:
        stmt = sqlite_insert(RateBucket).values(
            key_id=key_id, tokens=tokens, updated_at=now
        ).on_conflict_do_update(
            index_elements=[RateBucket.key_id],
            set_={"tokens": tokens, "updated_at": now},
        )
    session.execute(stmt)