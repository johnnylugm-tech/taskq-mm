"""[FR-01 / FR-02] Task business rules.

The service composes repository calls; it never touches SQLAlchemy
directly [NFR-06].
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from ..errors import ConflictError, NotFoundError, ValidationFailedError
from ..models.orm import Task, TaskResult, TaskStatus
from ..models.schemas import TaskCreate, TaskRead, TaskResultRead
from ..repository.task_repo import task_repo


class TaskNotFoundError(NotFoundError):
    """[FR-01] unknown task id."""

    def __init__(self) -> None:
        super().__init__(detail="Unknown task id.")


class DuplicateTaskNameError(ConflictError):
    """[FR-01] / §7 — name uniqueness."""

    def __init__(self, name: str) -> None:
        super().__init__(
            detail="A task with this name already exists.",
            extra={"name": name},
        )


@dataclass
class TaskPage:
    items: List[TaskRead]
    next_cursor: Optional[str]


def _to_read(task: Task) -> TaskRead:
    return TaskRead(
        id=task.id,
        command=task.command,
        name=task.name,
        status=task.status,
        created_at=task.created_at,
        updated_at=task.updated_at,
        tags=[t.label for t in task.tags],
    )


def create_task(session: Session, payload: TaskCreate) -> TaskRead:
    """Insert a task row [FR-01].

    ``payload`` has already passed pydantic validation; the service only
    enforces business-level uniqueness.
    """
    try:
        task = task_repo.create_task(
            session, command=payload.command, name=payload.name, tags=payload.tags
        )
    except task_repo.DuplicateNameError as exc:
        raise DuplicateTaskNameError(exc.args[0] if exc.args else payload.name) from exc
    # Re-fetch to populate the eager-loaded tags relationship.
    session.refresh(task)
    return _to_read(task)


def get_task(session: Session, task_id: str) -> TaskRead:
    """[FR-01] — single-task lookup.

    Unknown id raises :class:`TaskNotFoundError`. Authorisation must
    happen in the api layer *before* this call when the existence is
    sensitive [FR-04 / R4].
    """
    task = task_repo.find_task_by_id(session, task_id)
    if task is None:
        raise TaskNotFoundError()
    return _to_read(task)


def list_tasks(
    session: Session,
    *,
    status: Optional[TaskStatus] = None,
    limit: int = 50,
    cursor: Optional[str] = None,
) -> TaskPage:
    """[FR-01] — cursor pagination. ``limit`` must be 1..200."""
    if limit < 1 or limit > 200:
        raise ValidationFailedError(
            detail="limit must be between 1 and 200.",
            extra={"limit": limit},
        )
    rows, next_cursor = task_repo.list_tasks(session, status=status, limit=limit, cursor=cursor)
    return TaskPage(items=[_to_read(t) for t in rows], next_cursor=next_cursor)


def delete_task(session: Session, task_id: str) -> None:
    """[FR-01] — hard delete. Unknown id is silently ignored here so the
    api layer can decide whether to leak the existence [FR-04]."""
    removed = task_repo.delete_task(session, task_id)
    if not removed:
        raise TaskNotFoundError()


def record_run_for_task(
    session: Session,
    *,
    task_id: str,
    run_id: Optional[str],
    exit_code: Optional[int],
    stdout_tail: Optional[str],
    stderr_tail: Optional[str],
    duration_ms: Optional[int],
    started_at: datetime,
    finished_at: datetime,
) -> TaskResultRead:
    """[FR-02] — record a run and update the parent task status.

    Returns the persisted run row in the pydantic shape.
    """
    task = task_repo.find_task_by_id(session, task_id)
    if task is None:
        raise TaskNotFoundError()
    new_status = _derive_status(exit_code, duration_ms)
    task_repo.update_task_status(session, task, new_status)
    row = task_repo.record_run(
        session,
        task_id=task_id,
        run_id=run_id,
        exit_code=exit_code,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        duration_ms=duration_ms,
        started_at=started_at,
        finished_at=finished_at,
    )
    return TaskResultRead(
        id=row.id,
        run_id=row.run_id,
        exit_code=row.exit_code,
        stdout_tail=row.stdout_tail,
        stderr_tail=row.stderr_tail,
        duration_ms=row.duration_ms,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def _derive_status(exit_code: Optional[int], duration_ms: Optional[int]) -> TaskStatus:
    """Map an exit-code / duration pair to the final task status [FR-02]."""
    if duration_ms is None:
        return TaskStatus.FAILED
    if exit_code is None:
        return TaskStatus.TIMEOUT
    return TaskStatus.DONE if exit_code == 0 else TaskStatus.FAILED


def runs_for_task(session: Session, task_id: str) -> Tuple[List[TaskResultRead], bool]:
    """[FR-02] — list runs for a task; second element is ``found`` flag."""
    task = task_repo.find_task_by_id(session, task_id)
    if task is None:
        raise TaskNotFoundError()
    rows = task_repo.runs_for_task(session, task_id)
    return (
        [
            TaskResultRead(
                id=r.id,
                run_id=r.run_id,
                exit_code=r.exit_code,
                stdout_tail=r.stdout_tail,
                stderr_tail=r.stderr_tail,
                duration_ms=r.duration_ms,
                started_at=r.started_at,
                finished_at=r.finished_at,
            )
            for r in rows
        ],
        True,
    )