"""[FR-09 / NFR-12] FastAPI application factory.

The :func:`create_app` factory wires every component together. A single
:class:`BackgroundRunner` instance is stored on ``app.state.runner`` and
returned by :func:`get_runner` so the api handlers can enqueue work
[FR-08].
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import health as health_api
from .api import tasks as tasks_api
from .config import Settings, get_settings
from .errors import install_exception_handlers, new_correlation_id
from .logging_setup import configure_logging, get_logger
from .repository import session as session_repo
from .repository.session import transaction
from .service import tasks as tasks_service
from .service.runner import BackgroundRunner, ExecutionResult

_logger = get_logger("app")
_RUNNER: Optional[BackgroundRunner] = None


def get_runner() -> Optional[BackgroundRunner]:
    """Return the process-wide :class:`BackgroundRunner` [FR-08]."""
    return _RUNNER


def _make_recorder():
    """Build the recorder hook the runner uses to persist results."""
    async def _record(result: ExecutionResult) -> None:
        with transaction() as session:
            tasks_service.record_run_for_task(
                session,
                task_id=result.run_id,
                run_id=result.run_id,
                exit_code=result.exit_code,
                stdout_tail=result.stdout_tail,
                stderr_tail=result.stderr_tail,
                duration_ms=result.duration_ms,
                started_at=result.started_at,
                finished_at=result.finished_at,
            )
    return _record


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialise the engine + background runner on startup, drain on shutdown [FR-08]."""
    global _RUNNER
    configure_logging()
    session_repo.configure_engine()
    runner = BackgroundRunner(recorder=_make_recorder())
    await runner.start()
    _RUNNER = runner
    _logger.info("background runner started", extra={"max_concurrent": runner._max_concurrent})
    try:
        yield
    finally:
        await runner.close()
        _RUNNER = None


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """Build the FastAPI app — used by ``uvicorn taskq_api.app:app``."""
    cfg = settings or get_settings()
    app = FastAPI(
        title="taskq-api",
        version="1.0.0",
        lifespan=_lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    # [NFR-02] CORS deny-by-default — empty allowlist means nothing passes.
    origins = cfg.cors_origins_list()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["X-API-Key", "Content-Type"],
        allow_credentials=False,
    )
    install_exception_handlers(app)
    app.include_router(tasks_api.router)
    app.include_router(health_api.router)

    @app.middleware("http")
    async def _correlation_id_middleware(request, call_next):
        cid = request.headers.get("X-Correlation-Id") or new_correlation_id()
        request.state.correlation_id = cid
        response = await call_next(request)
        response.headers.setdefault("X-Correlation-Id", cid)
        return response

    return app


app = create_app()