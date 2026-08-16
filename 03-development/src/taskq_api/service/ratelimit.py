"""[FR-05] Per-token token-bucket rate limiting.

The bucket lives in the database so the cap is enforced consistently
across workers. Each call is wrapped in a single transaction with a
row-level lock so a worker cannot over-admit under contention [R12].
"""
from __future__ import annotations

from typing import Optional

from ..config import Settings, get_settings
from ..errors import RateLimitedError
from ..models.orm import APIKey
from ..repository.rate_repo import rate_repo
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
    """
    cfg = _settings(settings)
    key_row = session.get(APIKey, principal.key_id)
    if key_row is None:
        # Revoked mid-request — treat as authenticated but rate-limit refused.
        raise RateLimitedError(
            detail="Rate limit exceeded.",
            extra={"retry_after": 1},
        )
    allowed, retry_after = rate_repo.take_token(
        session,
        key_row,
        capacity=cfg.taskq_rate_burst,
        refill_per_sec=cfg.taskq_rate_per_sec,
    )
    if not allowed:
        seconds = max(1, int(retry_after + 0.999))
        raise RateLimitedError(
            detail="Rate limit exceeded.",
            extra={"retry_after": seconds},
        )