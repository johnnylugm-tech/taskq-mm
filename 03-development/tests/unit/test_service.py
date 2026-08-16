"""[FR-01 / FR-03 / FR-04 / FR-05] service-layer unit tests."""
from __future__ import annotations

import pytest

from taskq_api.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RateLimitedError,
    UnauthenticatedError,
    ValidationFailedError,
)
from taskq_api.models.orm import Scope, TaskStatus
from taskq_api.models.schemas import TaskCreate
from taskq_api.repository.key_repo import key_repo
from taskq_api.repository.task_repo import task_repo
from taskq_api.repository.session import transaction
from taskq_api.service import tasks as tasks_service
from taskq_api.service.auth import (
    Principal,
    authenticate,
    require_authenticated,
    require_scope,
    scope_satisfies,
)
from taskq_api.service.ratelimit import consume_or_raise


# --- auth ------------------------------------------------------------------


def test_scope_satisfies_hierarchy() -> None:
    assert scope_satisfies(Scope.READ, Scope.READ)
    assert scope_satisfies(Scope.WRITE, Scope.READ)
    assert scope_satisfies(Scope.ADMIN, Scope.READ)
    assert scope_satisfies(Scope.ADMIN, Scope.WRITE)
    assert not scope_satisfies(Scope.READ, Scope.WRITE)
    assert not scope_satisfies(Scope.WRITE, Scope.ADMIN)


def test_authenticate_unknown_key_returns_none() -> None:
    with transaction() as session:
        assert authenticate(session, "missing") is None


def test_authenticate_returns_principal() -> None:
    with transaction() as session:
        row, plaintext = key_repo.create_api_key(session, scope=Scope.WRITE)
    with transaction() as session:
        principal = authenticate(session, plaintext)
    assert isinstance(principal, Principal)
    assert principal.key_id == row.id
    assert principal.scope == Scope.WRITE


def test_require_scope_insufficient_raises_forbidden() -> None:
    principal = Principal(key_id=1, scope=Scope.READ)
    with pytest.raises(ForbiddenError):
        require_scope(principal, Scope.ADMIN)


def test_require_scope_ok() -> None:
    principal = Principal(key_id=1, scope=Scope.ADMIN)
    require_scope(principal, Scope.WRITE)  # no exception


def test_require_authenticated_raises_unauthenticated() -> None:
    with pytest.raises(UnauthenticatedError):
        require_authenticated(None)


def test_require_authenticated_returns_principal() -> None:
    p = Principal(key_id=1, scope=Scope.READ)
    assert require_authenticated(p) is p


# --- ratelimit -------------------------------------------------------------


def test_consume_or_raise_allows_under_burst() -> None:
    with transaction() as session:
        row, plaintext = key_repo.create_api_key(session, scope=Scope.WRITE)
        principal = Principal(key_id=row.id, scope=row.scope)
    consume_or_raise(session, principal)


def test_consume_or_raise_rejects_over_burst() -> None:
    with transaction() as session:
        row, plaintext = key_repo.create_api_key(session, scope=Scope.WRITE)
        principal = Principal(key_id=row.id, scope=row.scope)
    # Consume burst + one extra within a single transaction so the bucket
    # row stays locked and the SQLite test DB never serialises between
    # short-lived transactions.
    with transaction() as session:
        for _ in range(3):  # burst = 3 in test conftest
            consume_or_raise(session, principal)
        with pytest.raises(RateLimitedError):
            consume_or_raise(session, principal)


def test_consume_or_raise_unknown_key_raises() -> None:
    principal = Principal(key_id=99999, scope=Scope.READ)
    with transaction() as session:
        with pytest.raises(RateLimitedError):
            consume_or_raise(session, principal)


# --- tasks service ---------------------------------------------------------


def test_create_task_service() -> None:
    payload = TaskCreate(command="echo hi", name="svc-create")
    with transaction() as session:
        read = tasks_service.create_task(session, payload)
    assert read.name == "svc-create"
    assert read.status == TaskStatus.PENDING


def test_create_task_duplicate_raises_conflict() -> None:
    payload = TaskCreate(command="echo hi", name="dup-svc")
    with transaction() as session:
        tasks_service.create_task(session, payload)
    with pytest.raises(ConflictError):
        with transaction() as session:
            tasks_service.create_task(session, payload)


def test_get_task_unknown_raises_not_found() -> None:
    with pytest.raises(NotFoundError):
        with transaction() as session:
            tasks_service.get_task(session, "missing")


def test_get_task_returns_read() -> None:
    with transaction() as session:
        tasks_service.create_task(session, TaskCreate(command="echo hi", name="read-it"))
    with transaction() as session:
        rows, _ = task_repo.list_tasks(session)
        tid = rows[0].id
    with transaction() as session:
        read = tasks_service.get_task(session, tid)
    assert read.id == tid
    assert read.command == "echo hi"


def test_list_tasks_validates_limit() -> None:
    with pytest.raises(ValidationFailedError):
        with transaction() as session:
            tasks_service.list_tasks(session, limit=500)
    with pytest.raises(ValidationFailedError):
        with transaction() as session:
            tasks_service.list_tasks(session, limit=0)


def test_list_tasks_returns_page() -> None:
    for i in range(3):
        with transaction() as session:
            tasks_service.create_task(session, TaskCreate(command="echo a", name=f"svc-page-{i}"))
    with transaction() as session:
        page = tasks_service.list_tasks(session, limit=2)
    assert len(page.items) == 2


def test_delete_task_unknown_raises_not_found() -> None:
    with pytest.raises(NotFoundError):
        with transaction() as session:
            tasks_service.delete_task(session, "missing")


def test_record_run_updates_status() -> None:
    from datetime import datetime, timezone
    with transaction() as session:
        tasks_service.create_task(session, TaskCreate(command="echo", name="run-svc"))
    with transaction() as session:
        rows, _ = task_repo.list_tasks(session)
        tid = rows[0].id
    now = datetime.now(tz=timezone.utc)
    with transaction() as session:
        result = tasks_service.record_run_for_task(
            session,
            task_id=tid,
            run_id="run-1",
            exit_code=0,
            stdout_tail="ok",
            stderr_tail="",
            duration_ms=42,
            started_at=now,
            finished_at=now,
        )
    assert result.exit_code == 0
    with transaction() as session:
        read = tasks_service.get_task(session, tid)
    assert read.status == TaskStatus.DONE


def test_record_run_timeout_status() -> None:
    from datetime import datetime, timezone
    with transaction() as session:
        tasks_service.create_task(session, TaskCreate(command="sleep", name="run-timeout"))
    with transaction() as session:
        rows, _ = task_repo.list_tasks(session)
        tid = rows[0].id
    now = datetime.now(tz=timezone.utc)
    with transaction() as session:
        tasks_service.record_run_for_task(
            session,
            task_id=tid,
            run_id="r-timeout",
            exit_code=None,
            stdout_tail="",
            stderr_tail="killed",
            duration_ms=10000,
            started_at=now,
            finished_at=now,
        )
    with transaction() as session:
        read = tasks_service.get_task(session, tid)
    assert read.status == TaskStatus.TIMEOUT


def test_runs_for_task_unknown_raises() -> None:
    with pytest.raises(NotFoundError):
        with transaction() as session:
            tasks_service.runs_for_task(session, "missing")


def test_runs_for_task_returns_history() -> None:
    from datetime import datetime, timezone
    with transaction() as session:
        tasks_service.create_task(session, TaskCreate(command="echo", name="hist"))
    with transaction() as session:
        rows, _ = task_repo.list_tasks(session)
        tid = rows[0].id
    now = datetime.now(tz=timezone.utc)
    for i in range(2):
        with transaction() as session:
            tasks_service.record_run_for_task(
                session,
                task_id=tid,
                run_id=f"r-{i}",
                exit_code=0,
                stdout_tail="",
                stderr_tail="",
                duration_ms=10,
                started_at=now,
                finished_at=now,
            )
    with transaction() as session:
        runs, _ = tasks_service.runs_for_task(session, tid)
    assert len(runs) == 2