"""[FR-01 / FR-02] Task HTTP endpoints.

Every handler is thin: it composes service-layer calls and translates
business exceptions into the HTTP status code (the exceptions carry the
mapping). No business logic lives here.
"""
from __future__ import annotations

import asyncio
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status

from ..errors import NotReadyError
from ..models.orm import Scope, TaskStatus
from ..models.schemas import (
    CursorPage,
    TaskCreate,
    TaskRead,
    TaskResultRead,
    TaskRunRead,
)
from ..repository.session import transaction
from ..service.auth import Principal, require_scope
from ..service.runner import BackgroundRunner
from ..service import tasks as tasks_service
from .deps import CurrentPrincipal

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])


@router.post(
    "",
    response_model=TaskRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a task",
    description="[FR-01] Persist a new task. Requires write scope.",
)
def create_task_endpoint(
    payload: TaskCreate,
    principal: CurrentPrincipal,
) -> TaskRead:
    """[FR-01] POST /v1/tasks — body validated by ``TaskCreate`` pydantic model."""
    require_scope(principal, Scope.WRITE)
    with transaction() as session:
        return tasks_service.create_task(session, payload)


@router.get(
    "",
    response_model=CursorPage[TaskRead],
    summary="List tasks",
    description="[FR-01] Cursor-paginated list. Requires read scope.",
)
def list_tasks_endpoint(
    principal: CurrentPrincipal,
    status_filter: Annotated[Optional[TaskStatus], Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[Optional[str], Query()] = None,
) -> CursorPage[TaskRead]:
    """[FR-01] GET /v1/tasks — returns items + next_cursor."""
    require_scope(principal, Scope.READ)
    with transaction() as session:
        page = tasks_service.list_tasks(session, status=status_filter, limit=limit, cursor=cursor)
    return CursorPage[TaskRead](items=page.items, next_cursor=page.next_cursor)


@router.get(
    "/{task_id}",
    response_model=TaskRead,
    summary="Get a task by id",
    description="[FR-01] Returns full task row. Requires read scope.",
)
def get_task_endpoint(
    task_id: Annotated[str, Path(min_length=1)],
    principal: CurrentPrincipal,
) -> TaskRead:
    """[FR-01] GET /v1/tasks/{id}."""
    require_scope(principal, Scope.READ)
    with transaction() as session:
        return tasks_service.get_task(session, task_id)


@router.delete(
    "/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a task",
    description="[FR-01] Hard-delete the task and its run rows. Requires admin scope.",
)
def delete_task_endpoint(
    task_id: Annotated[str, Path(min_length=1)],
    principal: CurrentPrincipal,
) -> Response:
    """[FR-01] DELETE /v1/tasks/{id}."""
    require_scope(principal, Scope.ADMIN)
    with transaction() as session:
        tasks_service.delete_task(session, task_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{task_id}/run",
    response_model=TaskRunRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run a task",
    description="[FR-02] Enqueue an async execution; returns 202 with the new run id.",
)
async def run_task_endpoint(
    task_id: Annotated[str, Path(min_length=1)],
    principal: CurrentPrincipal,
) -> TaskRunRead:
    """[FR-02] POST /v1/tasks/{id}/run — 202 Accepted with run_id."""
    require_scope(principal, Scope.WRITE)
    with transaction() as session:
        task = tasks_service.get_task(session, task_id)
    runner: BackgroundRunner = _runner_from_state()
    run_id = await runner.submit(task.id, task.command)
    return TaskRunRead(task_id=task.id, run_id=run_id, status=task.status)


@router.get(
    "/{task_id}/runs",
    response_model=CursorPage[TaskResultRead],
    summary="List task runs",
    description="[FR-02] Returns the execution history for a task, newest first.",
)
def list_runs_endpoint(
    task_id: Annotated[str, Path(min_length=1)],
    principal: CurrentPrincipal,
) -> CursorPage[TaskResultRead]:
    """[FR-02] GET /v1/tasks/{id}/runs."""
    require_scope(principal, Scope.READ)
    with transaction() as session:
        rows, _ = tasks_service.runs_for_task(session, task_id)
    return CursorPage[TaskResultRead](items=rows, next_cursor=None)


def _runner_from_state() -> BackgroundRunner:
    """Read the BackgroundRunner off the FastAPI app state [FR-08]."""
    from ..app import get_runner  # late-bound to dodge an import cycle
    runner = get_runner()
    if runner is None:
        raise NotReadyError(detail="Background runner is not configured.")
    return runner