"""[FR-09] Health, readiness, and metrics endpoints."""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from ..config import get_settings
from ..errors import NotReadyError
from ..models.orm import Scope, TaskStatus
from ..repository.session import transaction
from ..service.auth import Principal, require_scope
from ..service.tasks import runs_for_task
from .deps import CurrentPrincipal

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="Liveness probe", description="[FR-09] Always 200 when the process is alive.")
def healthz() -> dict[str, str]:
    """[FR-09] ``GET /healthz`` — 200 ``{"status":"ok"}``."""
    return {"status": "ok"}


@router.get(
    "/readyz",
    summary="Readiness probe",
    description="[FR-09] 200 only when DB is reachable AND migration is at head; otherwise 503.",
)
def readyz(response: Response) -> dict[str, Any]:
    """[FR-09] ``GET /readyz`` — fail closed on missing migration."""
    detail: dict[str, Any] = {"db": "ok", "migration": "ok"}
    # DB ping
    try:
        with transaction() as session:
            session.execute(text("SELECT 1"))
    except Exception as exc:
        detail["db"] = f"unavailable: {type(exc).__name__}"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not-ready", **detail}
    # Migration at head
    head_revision = _read_alembic_head()
    current_revision = _read_alembic_current()
    if current_revision != head_revision:
        detail["migration"] = (
            f"behind head: current={current_revision} head={head_revision}"
        )
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not-ready", **detail}
    return {"status": "ok", **detail}


def _read_alembic_head() -> str | None:
    """Return the latest revision filename (or ``None`` if no versions)."""
    try:
        from alembic.script import ScriptDirectory
        from alembic.config import Config
        config = Config("alembic.ini")
        script = ScriptDirectory.from_config(config)
        return script.get_heads()[-1] if script.get_heads() else None
    except Exception:
        return None


def _read_alembic_current() -> str | None:
    """Return the current revision in the DB (or ``None`` if unversioned)."""
    db_url = get_settings().taskq_db_url
    try:
        from alembic.runtime.migration import MigrationContext
        from sqlalchemy import create_engine
        engine = create_engine(db_url, future=True)
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            rev = context.get_current_revision()
        engine.dispose()
        return rev
    except Exception:
        return None


@router.get(
    "/v1/metrics",
    summary="Service metrics",
    description="[FR-09] Task counts, run latency percentiles, rate-limit rejections.",
)
def metrics(principal: CurrentPrincipal) -> dict[str, Any]:
    """[FR-09] ``GET /v1/metrics`` — admin scope; redaction-safe [NFR-04]."""
    require_scope(principal, Scope.ADMIN)
    from ..models.schemas import MetricsResponse
    from sqlalchemy import func, select
    from ..models.orm import RateBucket, Task, TaskResult

    with transaction() as session:
        counts = dict(
            session.execute(
                select(Task.status, func.count(Task.id)).group_by(Task.status)
            ).all()
        )
        durations = [
            row.duration_ms
            for row in session.execute(
                select(TaskResult.duration_ms).where(TaskResult.duration_ms.is_not(None))
            ).all()
            if row.duration_ms is not None
        ]
        buckets = session.execute(select(RateBucket)).scalars().all()
    durations.sort()
    p50 = _percentile(durations, 50)
    p95 = _percentile(durations, 95)
    rate_limit_rejections = sum(1 for b in buckets if b.tokens < 1)
    payload = MetricsResponse(
        task_counts={str(k): int(v) for k, v in counts.items()},
        run_latency_ms={"p50": p50, "p95": p95, "count": len(durations)},
        rate_limit_rejections=int(rate_limit_rejections),
    ).model_dump()
    # [NFR-04] — ensure no password fragment from TASKQ_DB_URL leaks.
    safe_url = get_settings().safe_db_url()
    payload["db_url"] = safe_url
    return payload


def _percentile(values: list[int], pct: int) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    idx = max(0, min(len(values) - 1, int(round((pct / 100.0) * (len(values) - 1)))))
    return float(values[idx])