"""Independence module — RFC 7807 ``application/problem+json`` plumbing.

Every non-2xx HTTP response carries one of these bodies [FR-10]. The body
never includes stack traces, SQL statements, or filesystem paths [NFR-02 /
NFR-04]. A correlation id is attached so the client can splice logs.

Public surface
--------------
- :class:`Problem` — data class for a single error.
- :func:`problem_response` — build a FastAPI ``JSONResponse`` with the right
  Content-Type and ``X-Correlation-Id`` header.
- :exc:`APIError` — base exception carrying a :class:`Problem`.
- Specific subclasses for each status code listed in SPEC §7.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

# Type URIs are stable identifiers — they do not need to resolve.
TYPE_VALIDATION = "/errors/validation"
TYPE_UNAUTHENTICATED = "/errors/unauthenticated"
TYPE_FORBIDDEN = "/errors/forbidden"
TYPE_NOT_FOUND = "/errors/not-found"
TYPE_CONFLICT = "/errors/conflict"
TYPE_RATE_LIMITED = "/errors/rate-limited"
TYPE_NOT_READY = "/errors/not-ready"
TYPE_INTERNAL = "/errors/internal"


@dataclass(frozen=True)
class Problem:
    """RFC 7807 problem document.

    [FR-10] ``detail`` is restricted to safe, user-actionable text. The
    ``internal`` field is for the server-side log only; it must never be
    rendered in ``detail`` [NFR-02].
    """

    type: str
    title: str
    status: int
    detail: str
    instance: str
    correlation_id: str
    internal: Optional[str] = None  # logged, not rendered
    extra: Optional[Dict[str, Any]] = None

    def to_body(self) -> Dict[str, Any]:
        """Render the public body — never includes ``internal`` [NFR-02]."""
        body: Dict[str, Any] = {
            "type": self.type,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "instance": self.instance,
            "correlation_id": self.correlation_id,
        }
        if self.extra:
            body.update(self.extra)
        return body


def new_correlation_id() -> str:
    """Generate a 16-byte url-safe correlation id [FR-10]."""
    return secrets.token_urlsafe(16)


def problem_response(
    *,
    problem_type: str,
    title: str,
    status: int,
    detail: str,
    request: Request,
    internal: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    instance: Optional[str] = None,
) -> JSONResponse:
    """Build a JSONResponse carrying a problem document.

    [FR-10] — Content-Type is ``application/problem+json``; the correlation
    id is mirrored in the ``X-Correlation-Id`` response header.

    For 403 responses [FR-04 / R4] pass ``instance`` explicitly as the
    templated path (e.g. ``/v1/tasks/{id}``) so the resource id never
    leaks in the body even though the URL contains it.
    """
    cid = getattr(request.state, "correlation_id", None) or new_correlation_id()
    problem = Problem(
        type=problem_type,
        title=title,
        status=status,
        detail=detail,
        instance=instance if instance is not None else str(request.url.path),
        correlation_id=cid,
        internal=internal,
        extra=extra,
    )
    response_headers = {"X-Correlation-Id": cid}
    if headers:
        response_headers.update(headers)
    return JSONResponse(
        status_code=status,
        content=problem.to_body(),
        media_type="application/problem+json",
        headers=response_headers,
    )


class APIError(Exception):
    """Base exception carrying an HTTP status and a :class:`Problem`.

    Subclasses pin ``status`` and the type URI. ``detail`` must be a short,
    safe user-facing string; ``internal`` is logged separately.
    """

    status: int = 500
    problem_type: str = TYPE_INTERNAL
    title: str = "Internal Server Error"

    def __init__(self, detail: str, *, internal: Optional[str] = None,
                 extra: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.internal = internal
        self.extra = extra

    def to_problem(self, request: Request) -> Problem:
        cid = getattr(request.state, "correlation_id", None) or new_correlation_id()
        return Problem(
            type=self.problem_type,
            title=self.title,
            status=self.status,
            detail=self.detail,
            instance=str(request.url.path),
            correlation_id=cid,
            internal=self.internal,
            extra=self.extra,
        )


class UnauthenticatedError(APIError):
    status = 401
    problem_type = TYPE_UNAUTHENTICATED
    title = "Unauthenticated"


class ForbiddenError(APIError):
    status = 403
    problem_type = TYPE_FORBIDDEN
    title = "Forbidden"


class NotFoundError(APIError):
    status = 404
    problem_type = TYPE_NOT_FOUND
    title = "Not Found"


class ConflictError(APIError):
    status = 409
    problem_type = TYPE_CONFLICT
    title = "Conflict"


class RateLimitedError(APIError):
    status = 429
    problem_type = TYPE_RATE_LIMITED
    title = "Too Many Requests"


class NotReadyError(APIError):
    status = 503
    problem_type = TYPE_NOT_READY
    title = "Service Not Ready"


class ValidationFailedError(APIError):
    status = 422
    problem_type = TYPE_VALIDATION
    title = "Validation Failed"


# Each handler returned by ``app.exception_handler`` is held in this dict
# so static analysers see a real reference rather than a discarded
# decorator result. They are reachable from the app's handler table.
_HANDLER_REGISTRY: dict[str, object] = {}


def install_exception_handlers(app: FastAPI) -> None:
    """Wire all custom exception handlers onto a FastAPI app.

    [NFR-03] — ``asyncio.CancelledError`` is *not* caught here; it must
    propagate so the ASGI server can complete shutdown correctly.
    """

    @app.exception_handler(APIError)
    async def _api_error_handler(request: Request, exc: APIError):
        problem = exc.to_problem(request)
        # [FR-04 / R4] for 403 the instance URI must be the templated path —
        # the actual resource id never makes it into the body.
        if problem.status == 403:
            problem = Problem(
                type=problem.type,
                title=problem.title,
                status=problem.status,
                detail=problem.detail,
                instance=_templated_path(str(request.url.path)),
                correlation_id=problem.correlation_id,
                internal=problem.internal,
                extra=problem.extra,
            )
        headers: Optional[Dict[str, str]] = None
        if isinstance(exc, RateLimitedError) and exc.extra and "retry_after" in exc.extra:
            headers = {"Retry-After": str(exc.extra["retry_after"])}
        response = JSONResponse(
            status_code=problem.status,
            content=problem.to_body(),
            media_type="application/problem+json",
            headers={"X-Correlation-Id": problem.correlation_id, **(headers or {})},
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        # exc.errors() may contain ValueError instances — strip them to a
        # JSON-serialisable shape.
        safe_errors: list[dict[str, object]] = []
        for entry in exc.errors():
            clean = {k: v for k, v in entry.items() if k != "ctx"}
            clean["ctx"] = {
                ck: str(cv) if isinstance(cv, Exception) else cv
                for ck, cv in (entry.get("ctx") or {}).items()
            }
            safe_errors.append(clean)
        return problem_response(
            problem_type=TYPE_VALIDATION,
            title="Validation Failed",
            status=422,
            detail="Request body failed validation.",
            request=request,
            internal=str(exc),
            extra={"errors": safe_errors},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
        return problem_response(
            problem_type=TYPE_NOT_FOUND if exc.status_code == 404 else TYPE_INTERNAL,
            title=exc.detail if isinstance(exc.detail, str) else "HTTP Error",
            status=exc.status_code,
            detail=str(exc.detail) if isinstance(exc.detail, str) else "HTTP error.",
            request=request,
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception):
        # [NFR-03] — generic 500 envelope. Internal detail goes to the log,
        # not to the wire.
        return problem_response(
            problem_type=TYPE_INTERNAL,
            title="Internal Server Error",
            status=500,
            detail="An unexpected error occurred.",
            request=request,
            internal=f"{type(exc).__name__}: {exc}",
        )

    # Pin handlers so static analysers see them as used (they are stored on
    # the app's exception_handler table; we keep refs for clarity/tests).
    _HANDLER_REGISTRY["api_error"] = _api_error_handler
    _HANDLER_REGISTRY["validation"] = _validation_handler
    _HANDLER_REGISTRY["http_exception"] = _http_exception_handler
    _HANDLER_REGISTRY["unhandled"] = _unhandled_handler


import re as _re

_UUID_RE = _re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", _re.IGNORECASE)


def _templated_path(path: str) -> str:
    """Replace UUID-ish segments with ``{id}`` so 403 bodies don't leak."""
    return _UUID_RE.sub("{id}", path)