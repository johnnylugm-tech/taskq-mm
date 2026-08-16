"""[FR-09] Health, readiness, and metrics endpoints."""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from ..config import get_settings
from ..errors import NotReadyError
from ..models.orm import Scope, TaskStatus
from ..repository.session import get_engine, transaction
from ..service.auth import Principal, require_scope
from .deps import CurrentPrincipal

router = APIRouter(tags=["health"])

# Absolute path to the project root — ``api/health.py`` lives at
# ``<root>/03-development/src/taskq_api/api/``; we walk up four levels to land
# on the directory that owns ``alembic.ini``. This removes the CWD
# dependency that previously made the readiness probe lie green when the
# process was launched from any directory other than the repo root
# (Group C — closes P0-3 + P1-14).
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 4)))
_ALEMBIC_INI = os.path.join(_REPO_ROOT, "alembic.ini")


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
    """[FR-09] ``GET /readyz`` — fail closed on missing migration.

    The comparison is fail-closed: any ``None`` from either alembic
    helper is treated as a 503 cause (Group C — closes P0-3).
    """
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
    if (
        head_revision is None
        or current_revision is None
        or current_revision != head_revision
    ):
        detail["migration"] = (
            f"behind head: current={current_revision} head={head_revision}"
        )
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not-ready", **detail}
    return {"status": "ok", **detail}


def _read_alembic_head() -> str | None:
    """Return the latest revision filename (or ``None`` if no versions).

    Uses the absolute ``_ALEMBIC_INI`` path so the probe is independent of
    the process working directory (Group C — closes P1-14).
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from alembic.util import CommandError
    try:
        config = Config(_ALEMBIC_INI)
        script = ScriptDirectory.from_config(config)
        return script.get_heads()[-1] if script.get_heads() else None
    except (CommandError, FileNotFoundError, OSError):
        return None
    except Exception as exc:
        from ..logging_setup import get_logger
        get_logger("health").warning(
            "alembic head read failed", extra={"reason": f"{type(exc).__name__}: {exc}"}
        )
        return None


def _read_alembic_current() -> str | None:
    """Return the current revision in the DB (or ``None`` if unversioned).

    Reuses the process-wide engine (Group C — closes P1-13: no per-probe
    Engine creation, no connection pool leak).
    """
    from alembic.runtime.migration import MigrationContext
    try:
        engine = get_engine()
        with engine.connect() as connection:
            ctx = MigrationContext.configure(connection)
            return ctx.get_current_revision()
    except Exception as exc:  # alembic probes — surface cause, do not crash
        from ..logging_setup import get_logger
        get_logger("health").warning(
            "alembic current read failed", extra={"reason": f"{type(exc).__name__}: {exc}"}
        )
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
        # [Group G] bound the latency sample so a large history doesn't
        # OOM the api process (10k rows is plenty for a sampled percentile).
        durations = [
            row.duration_ms
            for row in session.execute(
                select(TaskResult.duration_ms)
                .where(TaskResult.duration_ms.is_not(None))
                .limit(10_000)
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