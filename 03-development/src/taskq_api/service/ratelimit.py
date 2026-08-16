"""[FR-05] Per-token token-bucket rate limiting.

The bucket lives in the database so the cap is enforced consistently
across workers. Each call is wrapped in a single transaction with a
row-level lock so a worker cannot over-admit under contention [R12].
"""
from __future__ import annotations

import math
from typing import Optional

from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..errors import RateLimitedError, UnauthenticatedError
from ..models.orm import APIKey
from ..repository.rate_repo import rate_repo
from ..repository.session import select_for_update_or_pass
from .auth import Principal


def _settings(settings: Optional[Settings]) -> Settings:
    return settings if settings is not None else get_settings()


def consume_or_raise(
    session: Session,
    principal: Principal,
    *,
    settings: Optional[Settings] = None,
) -> None:
    """Consume one token from the principal's bucket.

    On exhaustion, raises :class:`RateLimitedError` with a populated
    ``retry_after`` extra — the API layer turns it into a ``Retry-After``
    header.

    [Group D / P1-10] re-fetch the :class:`APIKey` inside this
    transaction with ``with_for_update()`` so a ``revoke_api_key`` that
    committed in another session is visible here. SQLAlchemy's identity
    map would otherwise return the cached row with ``revoked_at = NULL``.
    On SQLite ``with_for_update`` is a no-op — the test path is
    racy by design (the same caveat as the rest of the suite), but the
    fix is loud: a Postgres-only CI lane would close it.
    """
    cfg = _settings(settings)
    # Re-fetch with row lock (or plain select on SQLite) so a parallel
    # ``revoke_api_key`` is observed. We use the dev-path compromise
    # here because the APIKey lookup is a *read* that gates auth — the
    # row lock that matters for the rate-limit invariant is the
    # RateBucket lock in ``take_token`` itself.
    key_row = session.execute(
        select_for_update_or_pass(session, APIKey).where(APIKey.id == principal.key_id)
    ).scalar_one_or_none()
    if key_row is None or key_row.revoked_at is not None:
        # [Group D] raise UnauthenticatedError (not RateLimitedError) —
        # a revoked key is not a rate-limit decision, it's an auth one.
        raise UnauthenticatedError(detail="API key invalid or revoked.")
    allowed, retry_after = rate_repo.take_token(
        session,
        key_row,
        capacity=cfg.taskq_rate_burst,
        refill_per_sec=cfg.taskq_rate_per_sec,
    )
    if not allowed:
        # [Group G] use math.ceil — clearer than the +0.999 trick and
        # matches the stdlib's documented semantics.
        seconds = max(1, math.ceil(retry_after))
        raise RateLimitedError(
            detail="Rate limit exceeded.",
            extra={"retry_after": seconds},
        )