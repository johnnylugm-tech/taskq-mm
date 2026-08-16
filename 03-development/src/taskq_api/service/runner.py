"""[FR-02 / FR-08] Async subprocess executor.

Key invariants
--------------
- ``shell=True`` is never used; commands are split via ``shlex.split`` and
  passed positionally to :func:`asyncio.create_subprocess_exec`
  [NFR-02].
- Concurrency is bounded by ``TASKQ_MAX_CONCURRENT``. New tasks queue in
  an :class:`asyncio.Queue`; workers dequeue and run.
- ``asyncio.wait_for`` enforces the per-task timeout. On timeout the child
  process is killed and ``await process.wait()`` ensures the kernel
  reaps it before we move on [FR-08 / R8].
- ``asyncio.CancelledError`` propagates — it must never be swallowed by
  ``except Exception`` [NFR-03].
- Graceful drain on shutdown waits for in-flight work up to
  ``TASKQ_DRAIN_TIMEOUT``; overruns are reported via :class:`NotReadyError`
  so the api layer can mark the task ``interrupted``.
"""
from __future__ import annotations

import asyncio
import shlex
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from ..config import Settings, get_settings
from ..errors import NotReadyError, ValidationFailedError
from ..logging_setup import get_logger
from ..models.orm import TaskStatus
from ..redaction import redact_text

_logger = get_logger("runner")


@dataclass
class ExecutionResult:
    """The outcome of a single subprocess run [FR-02]."""

    task_id: str
    run_id: str
    exit_code: Optional[int]
    stdout_tail: str
    stderr_tail: str
    duration_ms: int
    status: TaskStatus
    started_at: datetime
    finished_at: datetime


# A hook called once per run with the captured result. The api layer uses
# this to persist the result inside the same transaction boundary.
RunRecorder = Callable[[ExecutionResult], Awaitable[None]]

# Tails are bounded so a misbehaving command cannot blow out the DB row.
_MAX_TAIL_BYTES = 4096


async def run_subprocess(
    command: str,
    *,
    timeout: float,
    now_fn: Optional[Callable[[], datetime]] = None,
) -> ExecutionResult:
    """Execute ``command`` once, returning an :class:`ExecutionResult` [FR-02].

    ``timeout`` is in seconds; pass ``TASKQ_TASK_TIMEOUT`` from the caller.
    ``now_fn`` is a test seam for deterministic timing.
    """
    if not command or not command.strip():
        raise ValidationFailedError(detail="command is empty.")
    argv = shlex.split(command)
    if not argv:  # pragma: no cover — defensive
        raise ValidationFailedError(detail="command produced no argv.")
    run_id = str(uuid.uuid4())
    now_factory = now_fn or (lambda: datetime.now(tz=timezone.utc))
    started_at = now_factory()
    started_monotonic = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    timed_out = False
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        timed_out = True
        await _kill_and_wait(proc)
        stdout_bytes, stderr_bytes = b"", b""
    finished_at = now_factory()
    duration_ms = int((time.monotonic() - started_monotonic) * 1000)
    stdout_tail = _truncate(stdout_bytes.decode("utf-8", "replace"))
    stderr_tail = _truncate(stderr_bytes.decode("utf-8", "replace"))
    # [NFR-04] redact before storage.
    stdout_tail = redact_text(stdout_tail)
    stderr_tail = redact_text(stderr_tail)
    if timed_out:
        status = TaskStatus.TIMEOUT
        exit_code: Optional[int] = None
    else:
        exit_code = proc.returncode
        status = TaskStatus.DONE if exit_code == 0 else TaskStatus.FAILED
    return ExecutionResult(
        task_id="",
        run_id=run_id,
        exit_code=exit_code,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        duration_ms=duration_ms,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
    )


async def _kill_and_wait(proc: asyncio.subprocess.Process) -> None:
    """Best-effort kill then reap [FR-08 / R8]."""
    if proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        try:
            await proc.wait()
        except Exception:  # pragma: no cover — extremely defensive
            _logger.warning("failed to reap child process", exc_info=False)


def _truncate(text: str) -> str:
    if len(text) <= _MAX_TAIL_BYTES:
        return text
    return text[-_MAX_TAIL_BYTES:]


class BackgroundRunner:
    """[FR-08] Bounded concurrency executor with graceful drain.

    Submit tasks with :meth:`submit`. On :meth:`close` the runner stops
    accepting new work, waits for in-flight workers up to
    ``TASKQ_DRAIN_TIMEOUT`` and abandons anything still running — those
    callers see :class:`NotReadyError` so the api layer can mark the task
    ``interrupted``.
    """

    def __init__(
        self,
        *,
        recorder: RunRecorder,
        settings: Optional[Settings] = None,
        max_concurrent: Optional[int] = None,
    ) -> None:
        cfg = settings or get_settings()
        self._settings = cfg
        self._max_concurrent = max_concurrent or cfg.taskq_max_concurrent
        self._drain_timeout = cfg.taskq_drain_timeout
        self._task_timeout = cfg.taskq_task_timeout
        self._recorder = recorder
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._closed = False
        self._inflight: set[tuple[str, str]] = set()
        self._inflight_lock = asyncio.Lock()

    async def start(self) -> None:
        """Spawn the worker tasks [FR-08]."""
        if self._workers:
            return
        for _ in range(self._max_concurrent):
            self._workers.append(asyncio.create_task(self._worker_loop()))

    async def submit(self, task_id: str, command: str) -> str:
        """Enqueue a task; returns once a worker picks it up."""
        if self._closed:
            raise NotReadyError(detail="Runner is shutting down; cannot accept new work.")
        await self._queue.put((task_id, command))
        return str(uuid.uuid4())

    async def _worker_loop(self) -> None:
        while True:
            try:
                task_id, command = await self._queue.get()
            except asyncio.CancelledError:  # pragma: no cover — exercised by the shutdown path test
                # [NFR-03] shutdown — propagate.
                raise
            if task_id == "__shutdown__":
                self._queue.task_done()
                return
            try:
                async with self._inflight_lock:
                    self._inflight.add((task_id, command))
                await self._dispatch(task_id, command)
            finally:
                async with self._inflight_lock:
                    self._inflight.discard((task_id, command))
                self._queue.task_done()

    async def _dispatch(self, task_id: str, command: str) -> None:
        try:
            result = await run_subprocess(command, timeout=self._task_timeout)
        except asyncio.CancelledError:
            # [NFR-03] — let it propagate.
            raise
        except ValidationFailedError:
            _logger.warning("invalid command rejected", extra={"task_id": task_id})
            return
        except Exception:  # noqa: BLE001
            _logger.error(
                "subprocess execution failed",
                extra={"task_id": task_id},
            )
            return
        # Stamp the originating task_id onto the result so recorders can
        # write into task_results without ambiguity [FR-02].
        result_with_task = ExecutionResult(
            task_id=task_id,
            run_id=result.run_id,
            exit_code=result.exit_code,
            stdout_tail=result.stdout_tail,
            stderr_tail=result.stderr_tail,
            duration_ms=result.duration_ms,
            status=result.status,
            started_at=result.started_at,
            finished_at=result.finished_at,
        )
        try:
            await self._recorder(result_with_task)
        except Exception:  # noqa: BLE001
            _logger.error(
                "recorder failed for task",
                extra={"task_id": task_id},
            )

    async def close(self) -> None:
        """Stop accepting work, drain in-flight workers [FR-08]."""
        if self._closed:
            return
        self._closed = True
        for _ in self._workers:
            await self._queue.put(("__shutdown__", ""))
        try:
            await asyncio.wait_for(self._queue.join(), timeout=self._drain_timeout)
        except asyncio.TimeoutError:
            _logger.warning("graceful drain exceeded; cancelling workers")
            for worker in self._workers:
                worker.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers.clear()
        else:
            for worker in self._workers:
                worker.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers.clear()

    @property
    def inflight_count(self) -> int:
        return len(self._inflight)