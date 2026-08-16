"""[FR-03 / FR-04 / NFR-02] Authentication and authorisation.

Scope hierarchy: ``read < write < admin``.

The service exposes:
- :func:`authenticate` — verify an ``X-API-Key`` header value and return
  a :class:`Principal` carrying the resolved scope.
- :func:`require_scope` — gate a resource. When the principal lacks the
  needed scope the function raises :class:`ForbiddenError` *without*
  revealing whether the resource exists [R4 / FR-04].
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..errors import ForbiddenError, UnauthenticatedError
from ..models.orm import APIKey, Scope
from ..repository.key_repo import key_repo

_SCOPE_ORDER: dict[Scope, int] = {Scope.READ: 0, Scope.WRITE: 1, Scope.ADMIN: 2}


def scope_satisfies(actual: Scope, required: Scope) -> bool:
    """Return ``True`` when ``actual`` is at least ``required`` [FR-04]."""
    return _SCOPE_ORDER[actual] >= _SCOPE_ORDER[required]


@dataclass(frozen=True)
class Principal:
    """The authenticated caller.

    Carried through request handlers so each handler can authorise
    independently without re-querying the database.
    """

    key_id: int
    scope: Scope


def authenticate(session: Session, plaintext: Optional[str]) -> Optional[Principal]:
    """Resolve ``X-API-Key`` to a :class:`Principal` or ``None``.

    ``None`` is returned for any missing / unknown / revoked key, never an
    exception — the API layer raises the 401 itself [FR-03].
    """
    if not plaintext:
        return None
    row: Optional[APIKey] = key_repo.fetch_active_api_key(session, plaintext)
    if row is None:
        return None
    return Principal(key_id=row.id, scope=row.scope)


def require_scope(principal: Principal, required: Scope) -> None:
    """Raise :class:`ForbiddenError` when ``principal`` lacks ``required``.

    [FR-04] — the message never mentions whether a resource exists.
    """
    if not scope_satisfies(principal.scope, required):
        raise ForbiddenError(detail="Insufficient scope for this resource.")


def _ensure_principal(principal: Optional[Principal]) -> Principal:
    if principal is None:
        raise UnauthenticatedError(detail="Missing or invalid API key.")
    return principal


def require_authenticated(principal: Optional[Principal]) -> Principal:
    """Raise :class:`UnauthenticatedError` if no principal is present."""
    return _ensure_principal(principal)