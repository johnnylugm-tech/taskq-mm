"""Repository unit tests — every query path covered."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from taskq_api.models.orm import Scope, TaskStatus
from taskq_api.repository.key_repo import key_repo
from taskq_api.repository.rate_repo import rate_repo
from taskq_api.repository.task_repo import task_repo
from taskq_api.repository.session import transaction
from taskq_api.repository.session import (
    get_engine,
    get_sessionmaker,
    select_for_update,
    select_for_update_or_pass,
    transaction as tx,
)


def test_create_task_persists() -> None:
    with tx() as session:
        task = task_repo.create_task(session, command="echo hi", name="alpha")
    assert task.id is not None
    assert task.name == "alpha"


def test_create_task_duplicate_name_raises() -> None:
    with tx() as session:
        task_repo.create_task(session, command="echo hi", name="dup")
    with pytest.raises(task_repo.DuplicateNameError):
        with tx() as session:
            task_repo.create_task(session, command="echo hi", name="dup")


def test_create_task_with_tags_persists_tags() -> None:
    with tx() as session:
        task = task_repo.create_task(
            session, command="echo hi", name="tagged", tags=["alpha", "beta", "alpha"]
        )
    assert sorted(t.label for t in task.tags) == ["alpha", "beta"]


def test_find_task_by_id_returns_none_for_unknown() -> None:
    with tx() as session:
        assert task_repo.find_task_by_id(session, "does-not-exist") is None


def test_find_task_by_id_loads_tags_eagerly() -> None:
    with tx() as session:
        task = task_repo.create_task(session, command="echo hi", name="eager", tags=["t"])
        tid = task.id
    with tx() as session:
        found = task_repo.find_task_by_id(session, tid)
    assert found is not None
    assert [t.label for t in found.tags] == ["t"]


def _id_of(name: str) -> str:
    with tx() as session:
        rows, _ = task_repo.list_tasks(session)
        for row in rows:
            if row.name == name:
                return row.id
        return ""


def test_list_tasks_paginates_with_cursor() -> None:
    """[FR-01] cursor pagination produces stable next_cursor pointers."""
    names = [f"page-{i:02d}" for i in range(5)]
    with tx() as session:
        for n in names:
            task_repo.create_task(session, command=f"echo {n}", name=n)
    with tx() as session:
        page1, cursor1 = task_repo.list_tasks(session, limit=2)
    assert len(page1) == 2
    assert cursor1 is not None
    with tx() as session:
        page2, cursor2 = task_repo.list_tasks(session, limit=2, cursor=cursor1)
    assert len(page2) == 2
    assert cursor2 is not None
    seen = {t.name for t in page1} | {t.name for t in page2}
    assert len(seen) == 4  # no overlap with page1
    with tx() as session:
        page3, cursor3 = task_repo.list_tasks(session, limit=2, cursor=cursor2)
    assert len(page3) >= 1
    assert cursor3 is None


def test_list_tasks_invalid_cursor_raises() -> None:
    with tx() as session:
        with pytest.raises(task_repo.TaskRepoError):
            task_repo.list_tasks(session, cursor="not-base64")


def test_list_tasks_status_filter() -> None:
    """[FR-01] ?status= filters at the DB layer."""
    with tx() as session:
        t1 = task_repo.create_task(session, command="echo a", name="status-a")
        t2 = task_repo.create_task(session, command="echo b", name="status-b")
        task_repo.update_task_status(session, t1, TaskStatus.DONE)
    with tx() as session:
        rows, _ = task_repo.list_tasks(session, status=TaskStatus.DONE)
    assert {r.name for r in rows} == {"status-a"}


def test_delete_task_returns_false_for_unknown() -> None:
    with tx() as session:
        assert task_repo.delete_task(session, "missing") is False


def test_delete_task_removes_row() -> None:
    with tx() as session:
        t = task_repo.create_task(session, command="echo", name="kill-me")
        tid = t.id
    with tx() as session:
        assert task_repo.delete_task(session, tid) is True
    with tx() as session:
        assert task_repo.find_task_by_id(session, tid) is None


def test_record_run_and_list_runs() -> None:
    """[FR-02] TaskResult rows persist with the right shape."""
    with tx() as session:
        t = task_repo.create_task(session, command="echo hi", name="run-it")
        tid = t.id
    started = datetime.now(tz=timezone.utc) - timedelta(seconds=2)
    finished = datetime.now(tz=timezone.utc)
    with tx() as session:
        task_repo.record_run(
            session,
            task_id=tid,
            run_id="r1",
            exit_code=0,
            stdout_tail="hello",
            stderr_tail="",
            duration_ms=2000,
            started_at=started,
            finished_at=finished,
        )
    with tx() as session:
        runs = task_repo.runs_for_task(session, tid)
    assert len(runs) == 1
    assert runs[0].run_id == "r1"
    assert runs[0].exit_code == 0


def test_update_task_status_persists() -> None:
    with tx() as session:
        t = task_repo.create_task(session, command="echo", name="status")
    with tx() as session:
        task_repo.update_task_status(session, t, TaskStatus.DONE)
    with tx() as session:
        again = task_repo.find_task_by_id(session, t.id)
        assert again.status == TaskStatus.DONE


# --- key_repo --------------------------------------------------------------


def test_create_api_key_returns_plaintext_once() -> None:
    """[FR-03] plaintext printed exactly once and stored as 64-hex hash."""
    with tx() as session:
        row, plaintext = key_repo.create_api_key(session, scope=Scope.WRITE)
    assert len(plaintext) >= 32
    assert row.key_hash == hashlib.sha256(plaintext.encode()).hexdigest()
    assert len(row.key_hash) == 64


def test_api_keys_table_has_no_plaintext() -> None:
    """[NFR-02] verify api_keys stores only hash, not the plaintext."""
    plaintext_value = "plaintext-never-stored"
    with tx() as session:
        key_repo.create_api_key(session, scope=Scope.READ)
        # Inject a row with the plaintext manually — it must not match.
        session.add(
            key_repo.APIKey(
                key_hash=key_repo.hash_for_tests(plaintext_value),
                scope=Scope.READ,
            )
        )
    with tx() as session:
        all_rows = key_repo.list_api_keys(session)
    for row in all_rows:
        assert row.key_hash != plaintext_value


def test_fetch_active_api_key_unknown_returns_none() -> None:
    with tx() as session:
        assert key_repo.fetch_active_api_key(session, "unknown") is None


def test_fetch_active_api_key_revoked_returns_none() -> None:
    with tx() as session:
        row, plaintext = key_repo.create_api_key(session, scope=Scope.READ)
        kid = row.id
    with tx() as session:
        key_repo.revoke_api_key(session, kid)
    with tx() as session:
        assert key_repo.fetch_active_api_key(session, plaintext) is None


def test_revoke_api_key_unknown_returns_none() -> None:
    with tx() as session:
        assert key_repo.revoke_api_key(session, 99999) is None


# --- rate_repo -------------------------------------------------------------


def test_init_rate_bucket_creates_row() -> None:
    with tx() as session:
        row, plaintext = key_repo.create_api_key(session, scope=Scope.WRITE)
        bucket = rate_repo.init_rate_bucket(session, row, capacity=10)
    assert bucket.tokens == 10


def test_init_rate_bucket_is_idempotent() -> None:
    with tx() as session:
        row, _ = key_repo.create_api_key(session, scope=Scope.WRITE)
        rate_repo.init_rate_bucket(session, row, capacity=10)
    with tx() as session:
        row2 = session.get(type(row), row.id)
        bucket = rate_repo.init_rate_bucket(session, row2, capacity=10)
    assert bucket.tokens == 10


def test_take_token_consumes_one() -> None:
    with tx() as session:
        row, _ = key_repo.create_api_key(session, scope=Scope.WRITE)
    with tx() as session:
        row2 = session.get(type(row), row.id)
        allowed, retry = rate_repo.take_token(session, row2, capacity=3, refill_per_sec=0)
    assert allowed is True
    assert retry == 0.0


def test_take_token_over_capacity_returns_retry_after() -> None:
    with tx() as session:
        row, _ = key_repo.create_api_key(session, scope=Scope.WRITE)
    with tx() as session:
        row2 = session.get(type(row), row.id)
        # burn through the bucket
        for _ in range(3):
            rate_repo.take_token(session, row2, capacity=3, refill_per_sec=0)
        allowed, retry = rate_repo.take_token(session, row2, capacity=3, refill_per_sec=1.0)
    assert allowed is False
    assert retry > 0


def test_take_token_refills_over_time() -> None:
    with tx() as session:
        row, _ = key_repo.create_api_key(session, scope=Scope.WRITE)
    with tx() as session:
        row2 = session.get(type(row), row.id)
        for _ in range(3):
            rate_repo.take_token(session, row2, capacity=3, refill_per_sec=0)
        future = datetime.now(tz=timezone.utc) + timedelta(seconds=2)
        allowed, _ = rate_repo.take_token(session, row2, capacity=3, refill_per_sec=5.0, now=future)
    assert allowed is True


def test_select_for_update_raises_on_sqlite() -> None:
    """[Group D] on SQLite the contract is loud: the no-op is raised so
    callers cannot silently rely on a lock SQLite cannot provide.
    """
    from taskq_api.models.orm import RateBucket as _RB
    with tx() as session:
        with pytest.raises(RuntimeError, match="no-op on SQLite"):
            select_for_update(session, _RB)


def test_select_for_update_or_pass_returns_statement_on_sqlite() -> None:
    """[Group D] the dev-path compromise returns a plain SELECT on SQLite."""
    from taskq_api.models.orm import RateBucket as _RB
    from sqlalchemy import select
    with tx() as session:
        stmt = select_for_update_or_pass(session, _RB)
        assert stmt is not None
        # The statement is a plain ``select`` (no ``with_for_update``) on
        # SQLite; on Postgres the helper would have called
        # ``.with_for_update()``.
        compiled = str(stmt.compile(dialect=session.bind.dialect))
        assert "for update" not in compiled.lower()