"""Task / TaskResult / Tag persistence [FR-01 / FR-02 / FR-06].

All public functions accept a :class:`Session` so callers compose the
transaction boundary themselves (the caller uses
:func:`taskq_api.repository.session.transaction` for one Session per
request [FR-06]).
"""
from __future__ import annotations

import base64
import uuid
from datetime import datetime
from typing import List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..models.orm import Tag, Task, TaskResult, TaskStatus


# Self-reference for service-layer import ergonomics [NFR-06].
import importlib as _importlib
task_repo = _importlib.import_module(__name__)

# Sentinel row used by cursor pagination [FR-01].
_CURSOR_SEP = "::"


class TaskRepoError(Exception):
    """Base class for task repository errors."""


class DuplicateNameError(TaskRepoError):
    """[FR-01] name uniqueness violation."""


def _encode_cursor(created_at: datetime, task_id: str) -> str:
    raw = f"{created_at.isoformat()}{_CURSOR_SEP}{task_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> Tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts, task_id = raw.split(_CURSOR_SEP, 1)
        return datetime.fromisoformat(ts), task_id
    except Exception as exc:
        raise TaskRepoError("invalid cursor") from exc


def create_task(
    session: Session,
    *,
    command: str,
    name: str,
    tags: Optional[List[str]] = None,
) -> Task:
    """Insert a new :class:`Task` row [FR-01].

    Tags are upserted: any labels not yet present are created on the fly.
    Raises :class:`DuplicateNameError` on a ``uq_tasks_name`` violation.
    """
    task = Task(command=command, name=name, status=TaskStatus.PENDING)
    if tags:
        task.tags = _upsert_tags(session, tags)
    session.add(task)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise DuplicateNameError(name) from exc
    return task


def _upsert_tags(session: Session, labels: List[str]) -> List[Tag]:
    """Idempotently resolve ``labels`` to a list of :class:`Tag` instances."""
    seen: set[str] = set()
    cleaned: List[str] = []
    for label in labels:
        stripped = label.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        cleaned.append(stripped)
    if not cleaned:
        return []
    existing = session.execute(select(Tag).where(Tag.label.in_(cleaned))).scalars().all()
    existing_by_label = {t.label: t for t in existing}
    result: List[Tag] = []
    for label in cleaned:
        if label in existing_by_label:
            result.append(existing_by_label[label])
        else:
            tag = Tag(label=label)
            session.add(tag)
            result.append(tag)
    session.flush()
    return result


def find_task_by_id(session: Session, task_id: str) -> Optional[Task]:
    """Return the task with ``task_id`` or ``None``.

    Uses :func:`selectinload` to eager-load ``tags`` so the api handler
    can serialise without an N+1 fetch [NFR-01].
    """
    return session.execute(
        select(Task).where(Task.id == task_id).options(selectinload(Task.tags))
    ).scalar_one_or_none()


def list_tasks(
    session: Session,
    *,
    status: Optional[TaskStatus] = None,
    limit: int = 50,
    cursor: Optional[str] = None,
) -> Tuple[List[Task], Optional[str]]:
    """Cursor-paginated task list [FR-01 / NFR-01].

    ``limit`` is clamped by the caller (handlers enforce 1 ≤ limit ≤ 200).
    Returns ``(items, next_cursor)`` — ``next_cursor`` is ``None`` when
    the page is exhausted.
    """
    stmt = select(Task).options(selectinload(Task.tags)).order_by(
        Task.created_at.desc(), Task.id.desc()
    )
    if status is not None:
        stmt = stmt.where(Task.status == status)
    if cursor:
        ts, last_id = _decode_cursor(cursor)
        stmt = stmt.where(
            (Task.created_at < ts) | ((Task.created_at == ts) & (Task.id < last_id))
        )
    stmt = stmt.limit(limit + 1)
    rows = session.execute(stmt).scalars().all()
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = _encode_cursor(last.created_at, last.id)
        rows = rows[:limit]
    else:
        next_cursor = None
    return list(rows), next_cursor


def delete_task(session: Session, task_id: str) -> bool:
    """Delete the task with ``task_id``. Returns ``True`` when a row was removed."""
    task = session.get(Task, task_id)
    if task is None:
        return False
    session.delete(task)
    session.flush()
    return True


def update_task_status(session: Session, task: Task, status: TaskStatus) -> None:
    """Set ``task.status`` [FR-02]."""
    task.status = status
    session.add(task)
    session.flush()


def record_run(
    session: Session,
    *,
    task_id: str,
    run_id: Optional[str] = None,
    exit_code: Optional[int],
    stdout_tail: Optional[str],
    stderr_tail: Optional[str],
    duration_ms: Optional[int],
    started_at: datetime,
    finished_at: datetime,
) -> TaskResult:
    """Persist a :class:`TaskResult` row [FR-02 / v3 schema]."""
    result = TaskResult(
        task_id=task_id,
        run_id=run_id or str(uuid.uuid4()),
        exit_code=exit_code,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        duration_ms=duration_ms,
        started_at=started_at,
        finished_at=finished_at,
    )
    session.add(result)
    session.flush()
    return result


def runs_for_task(session: Session, task_id: str) -> List[TaskResult]:
    """Return runs for a task, newest first [FR-02]."""
    return list(
        session.execute(
            select(TaskResult)
            .where(TaskResult.task_id == task_id)
            .order_by(TaskResult.finished_at.desc(), TaskResult.id.desc())
        ).scalars()
    )