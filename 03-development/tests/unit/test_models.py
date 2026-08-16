"""[FR-01] pydantic validation + ORM model shape."""
from __future__ import annotations

import pytest

from taskq_api.models.orm import (
    APIKey,
    Base,
    RateBucket,
    Scope,
    Tag,
    Task,
    TaskResult,
    TaskStatus,
)
from taskq_api.models.schemas import TaskCreate, TaskRead


def test_task_create_minimal_ok() -> None:
    payload = TaskCreate(command="echo hi", name="t1")
    assert payload.command == "echo hi"
    assert payload.name == "t1"
    assert payload.tags == []


def test_task_create_rejects_empty_command() -> None:
    with pytest.raises(ValueError):
        TaskCreate(command="", name="t1")


def test_task_create_rejects_overlong_name() -> None:
    with pytest.raises(ValueError):
        TaskCreate(command="echo", name="x" * 1001)


def test_task_create_rejects_injection_chars_in_command() -> None:
    """[FR-01] command may not contain shell metacharacters."""
    for bad in (";rm", "&&echo", "|cat", "$IFS", "`uname`", "\nrm", "\\rm"):
        with pytest.raises(ValueError):
            TaskCreate(command=f"echo {bad}", name="t")


def test_task_create_rejects_injection_chars_in_name() -> None:
    for bad in (";rm", "$IFS", "\n"):
        with pytest.raises(ValueError):
            TaskCreate(command="echo hi", name=f"name{bad}x")


def test_task_create_dedupes_tags() -> None:
    payload = TaskCreate(command="echo hi", name="t1", tags=["a", "a", "b"])
    assert payload.tags == ["a", "b"]


def test_task_create_rejects_oversized_tag() -> None:
    with pytest.raises(ValueError):
        TaskCreate(command="echo hi", name="t1", tags=["x" * 70])


def test_task_read_round_trip() -> None:
    """[FR-01] TaskRead accepts every field the api renders."""
    read = TaskRead(
        id="abc",
        command="echo hi",
        name="t1",
        status=TaskStatus.PENDING,
        created_at="2026-07-30T00:00:00Z",  # type: ignore[arg-type]
        updated_at="2026-07-30T00:00:00Z",  # type: ignore[arg-type]
        tags=[],
    )
    assert read.id == "abc"
    assert read.status == TaskStatus.PENDING


def test_all_orm_classes_in_metadata() -> None:
    """[FR-07] Alembic compares against Base.metadata — every model must be declared."""
    tables = {t.name for t in Base.metadata.tables.values()}
    assert {"tasks", "api_keys", "rate_buckets", "tags", "task_tags", "task_results"}.issubset(tables)


def test_scope_values() -> None:
    assert {s.value for s in Scope} == {"read", "write", "admin"}


def test_task_status_values() -> None:
    assert {s.value for s in TaskStatus} == {"pending", "running", "done", "failed", "timeout", "interrupted"}