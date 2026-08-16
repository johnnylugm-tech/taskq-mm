"""Per-token token bucket persistence [FR-05 / NFR-01].

Bucket state is stored in the database so the limit is consistent across
worker processes. The read-modify-write sequence runs inside a single
transaction with a row-level lock (``SELECT ... FOR UPDATE``) to avoid
race over-admission [R12 / FR-05].
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.orm import APIKey, RateBucket
from .session import select_for_update


# Self-reference for service-layer import ergonomics [NFR-06].
import importlib as _importlib
rate_repo = _importlib.import_module(__name__)


def init_rate_bucket(session: Session, key: APIKey, *, capacity: int) -> RateBucket:
    """Seed a bucket at full capacity if none exists yet [FR-05]."""
    existing = session.get(RateBucket, key.id)
    if existing is not None:
        return existing
    bucket = RateBucket(key_id=key.id, tokens=capacity, updated_at=datetime.now(tz=timezone.utc))
    session.add(bucket)
    session.flush()
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
    """
    now = now or datetime.now(tz=timezone.utc)
    bucket = session.execute(
        select_for_update(session, RateBucket).where(RateBucket.key_id == key.id)
    ).scalar_one_or_none()
    if bucket is None:
        bucket = init_rate_bucket(session, key, capacity=capacity)
        bucket = session.execute(
            select_for_update(session, RateBucket).where(RateBucket.key_id == key.id)
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