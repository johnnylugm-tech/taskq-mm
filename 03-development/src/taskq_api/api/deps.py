"""[FR-03 / FR-04] Single authentication + authorisation dependency.

Every ``/v1`` route declares ``Depends(current_principal)``. The dependency
opens a Session, runs authentication, applies the rate limit, and returns
a :class:`Principal`. The route handler then calls
:func:`taskq_api.service.auth.require_scope` for fine-grained checks.

[FR-04] — scope checks happen *before* the resource is fetched so 403
responses do not leak existence.
"""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import Depends, Header, Request

from ..errors import UnauthenticatedError
from ..repository.session import transaction
from ..service.auth import Principal, authenticate
from ..service.ratelimit import consume_or_raise

API_KEY_HEADER = "X-API-Key"


def get_principal(
    request: Request,
    x_api_key: Annotated[Optional[str], Header(alias=API_KEY_HEADER)] = None,
) -> Principal:
    """Authenticate the caller, rate-limit, and attach the principal to ``request.state``.

    Raises :class:`UnauthenticatedError` (401) or :class:`RateLimitedError`
    (429) via the global exception handlers.
    """
    with transaction() as session:
        principal = authenticate(session, x_api_key)
        if principal is None:
            raise UnauthenticatedError(detail="Missing or invalid API key.")
        request.state.principal = principal
        consume_or_raise(session, principal)
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]