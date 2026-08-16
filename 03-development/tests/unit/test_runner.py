"""[FR-02 / FR-08 / NFR-03] async runner unit tests."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

import pytest

from taskq_api.errors import NotReadyError, ValidationFailedError
from taskq_api.models.orm import TaskStatus
from taskq_api.repository.task_repo import task_repo
from taskq_api.repository.session import transaction
from taskq_api.service.runner import (
    BackgroundRunner,
    ExecutionResult,
    run_subprocess,
)


@pytest.mark.asyncio
async def test_run_subprocess_echo_succeeds() -> None:
    cmd = f'"{sys.executable}" -c "print(123)"'
    result = await run_subprocess(cmd, timeout=5.0)
    assert isinstance(result, ExecutionResult)
    assert result.exit_code == 0
    assert "123" in result.stdout_tail
    assert result.status == TaskStatus.DONE


@pytest.mark.asyncio
async def test_run_subprocess_nonzero_exit_marks_failed() -> None:
    cmd = f'"{sys.executable}" -c "import sys; sys.exit(2)"'
    result = await run_subprocess(cmd, timeout=5.0)
    assert result.exit_code == 2
    assert result.status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_run_subprocess_empty_command_rejected() -> None:
    with pytest.raises(ValidationFailedError):
        await run_subprocess("", timeout=1.0)


@pytest.mark.asyncio
async def test_run_subprocess_whitespace_command_rejected() -> None:
    with pytest.raises(ValidationFailedError):
        await run_subprocess("   ", timeout=1.0)


@pytest.mark.asyncio
async def test_run_subprocess_timeout_kills_child() -> None:
    """[FR-08 / R8] timeout must terminate the child — no orphan."""
    cmd = f'"{sys.executable}" -c "import time; time.sleep(10)"'
    result = await run_subprocess(cmd, timeout=0.5)
    assert result.status == TaskStatus.TIMEOUT
    assert result.exit_code is None


@pytest.mark.asyncio
async def test_run_subprocess_redacts_secrets_in_stdout() -> None:
    """[NFR-04] secrets captured in stdout must be redacted before storage."""
    cmd = f'"{sys.executable}" -c "print(\'token=abcdefghijklmnop\')"'
    result = await run_subprocess(cmd, timeout=2.0)
    assert "abcdefghijklmnop" not in result.stdout_tail
    assert "[REDACTED]" in result.stdout_tail


@pytest.mark.asyncio
async def test_run_subprocess_redacts_secrets_in_stderr() -> None:
    cmd = f'"{sys.executable}" -c "import sys; sys.stderr.write(\'Bearer xyz987654321\\n\')"'
    result = await run_subprocess(cmd, timeout=2.0)
    assert "xyz987654321" not in result.stderr_tail


# --- BackgroundRunner ----------------------------------------------------


@pytest.mark.asyncio
async def test_background_runner_executes_one_command() -> None:
    """[FR-08] a queued command runs end-to-end."""
    recorded: list[ExecutionResult] = []

    async def _rec(result: ExecutionResult) -> None:
        recorded.append(result)

    runner = BackgroundRunner(recorder=_rec, max_concurrent=1)
    await runner.start()
    cmd = f'"{sys.executable}" -c "print(42)"'
    run_id = await runner.submit("t1", cmd)
    await runner.close()
    assert len(recorded) == 1
    assert recorded[0].exit_code == 0
    assert "42" in recorded[0].stdout_tail


@pytest.mark.asyncio
async def test_background_runner_submit_after_close_raises() -> None:
    async def _rec(_: ExecutionResult) -> None:
        pass
    runner = BackgroundRunner(recorder=_rec, max_concurrent=1)
    await runner.start()
    await runner.close()
    with pytest.raises(NotReadyError):
        await runner.submit("t", "echo")


@pytest.mark.asyncio
async def test_background_runner_drains_inflight_then_exits() -> None:
    """[FR-08] graceful drain waits for in-flight workers before closing."""
    completed: list[str] = []
    async def _rec(result: ExecutionResult) -> None:
        completed.append(result.run_id)
    runner = BackgroundRunner(recorder=_rec, max_concurrent=2)
    await runner.start()
    for i in range(3):
        await runner.submit(f"t-{i}", f'"{sys.executable}" -c "print({i})"')
    await runner.close()
    assert len(completed) == 3


@pytest.mark.asyncio
async def test_background_runner_close_idempotent() -> None:
    async def _rec(_: ExecutionResult) -> None:
        pass
    runner = BackgroundRunner(recorder=_rec, max_concurrent=1)
    await runner.start()
    await runner.close()
    await runner.close()  # second close is a no-op


@pytest.mark.asyncio
async def test_background_runner_handles_recorder_failure() -> None:
    """[FR-08] recorder failure does not crash the worker."""
    async def _bad_rec(_: ExecutionResult) -> None:
        raise RuntimeError("boom")
    runner = BackgroundRunner(recorder=_bad_rec, max_concurrent=1)
    await runner.start()
    await runner.submit("t", f'"{sys.executable}" -c "print(1)"')
    # Let the worker drain.
    await asyncio.sleep(0.2)
    await runner.close()


@pytest.mark.asyncio
async def test_background_runner_records_in_db() -> None:
    """[FR-08 / FR-02] the recorder persists into task_results via the service layer."""
    from taskq_api.service import tasks as tasks_service
    from taskq_api.models.schemas import TaskCreate

    with transaction() as session:
        tasks_service.create_task(session, TaskCreate(command="echo", name="rec-via-db"))
    with transaction() as session:
        rows, _ = task_repo.list_tasks(session)
        tid = rows[0].id

    async def _rec(result: ExecutionResult) -> None:
        with transaction() as session:
            tasks_service.record_run_for_task(
                session,
                task_id=tid,
                run_id=result.run_id,
                exit_code=result.exit_code,
                stdout_tail=result.stdout_tail,
                stderr_tail=result.stderr_tail,
                duration_ms=result.duration_ms,
                started_at=result.started_at,
                finished_at=result.finished_at,
            )

    runner = BackgroundRunner(recorder=_rec, max_concurrent=1)
    await runner.start()
    await runner.submit(tid, f'"{sys.executable}" -c "print(99)"')
    await runner.close()
    with transaction() as session:
        runs, _ = tasks_service.runs_for_task(session, tid)
    assert any(r.exit_code == 0 for r in runs)