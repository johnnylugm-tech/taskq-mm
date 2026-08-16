"""API key persistence [FR-03 / NFR-02].

Only the SHA-256 hash is stored. The plaintext is returned *once* on
creation and never persisted.
"""
from __future__ import annotations

# Self-reference so callers can ``from taskq_api.repository.key_repo
# import key_repo`` and then write ``key_repo.create_api_key(...)``.
# Keeps the repository layer callable from the service layer without
# ``repository/__init__.py`` re-exporting the whole module [NFR-06].
import importlib as _importlib
key_repo = _importlib.import_module(__name__)

import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from typing import Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.orm import APIKey, Scope


def _hash_key(plaintext: str) -> str:
    """SHA-256 → 64 hex chars [FR-03 / NFR-02]."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_plaintext_key() -> str:
    """Generate a high-entropy random API key string."""
    return secrets.token_urlsafe(32)


def create_api_key(session: Session, *, scope: Scope) -> tuple[APIKey, str]:
    """Insert a new :class:`APIKey` row and return ``(row, plaintext)``.

    The plaintext is returned exactly once [FR-03].
    """
    plaintext = generate_plaintext_key()
    row = APIKey(key_hash=_hash_key(plaintext), scope=scope)
    session.add(row)
    session.flush()
    return row, plaintext


def fetch_active_api_key(session: Session, plaintext: str) -> Optional[APIKey]:
    """Look up the key by comparing the SHA-256 hash in constant time.

    Returns ``None`` for any unknown or revoked key. ``hmac.compare_digest``
    keeps the comparison timing-safe [FR-03 / NFR-02].
    """
    if not plaintext:
        return None
    target = _hash_key(plaintext)
    candidates: Iterable[APIKey] = session.execute(
        select(APIKey).where(APIKey.revoked_at.is_(None))
    ).scalars()
    for candidate in candidates:
        if hmac.compare_digest(candidate.key_hash, target):
            return candidate
    return None


def list_api_keys(session: Session) -> List[APIKey]:
    """Return every key, including revoked ones [FR-03]. Used by /v1/metrics."""
    return list(session.execute(select(APIKey).order_by(APIKey.id.desc())).scalars())


def revoke_api_key(session: Session, key_id: int) -> Optional[APIKey]:
    """Mark a key as revoked [FR-03]. Returns ``None`` if the id is unknown."""
    row = session.get(APIKey, key_id)
    if row is None:
        return None
    row.revoked_at = datetime.now(tz=timezone.utc)
    session.add(row)
    session.flush()
    return row


def hash_for_tests(plaintext: str) -> str:
    """Test-only helper exposing the SHA-256 hash [NFR-09]."""
    return _hash_key(plaintext)