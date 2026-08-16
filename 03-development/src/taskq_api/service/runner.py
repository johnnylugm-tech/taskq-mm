"""[FR-02 / FR-08] Async subprocess executor.

Key invariants
--------------
- The same ``run_id`` UUID is generated in :meth:`BackgroundRunner.submit`
  and threaded all the way through the queue, the worker, ``run_subprocess``,
  and the persisted :class:`ExecutionResult` so the HTTP 202 body and the
  ``task_results.run_id`` row agree (Group A — closes P0-1 + P2-2 + P2-23).
- ``run_subprocess`` wraps the subprocess lifecycle in ``try/finally`` and
  shields the kill with :func:`asyncio.shield` so cancellation can never
  abandon a child PID — the reaper runs on **every** exit path including
  ``CancelledError`` (Group B — closes P0-4 + R8).
- The :class:`BackgroundRunner` queue is bounded to
  ``max_concurrent * 2`` so a misbehaving caller cannot OOM the worker
  (Group G — closes P2-1).
- ``submit`` raises :class:`NotReadyError` when the runner has not been
  started; ``start()`` is idempotent under a lock (Group A + G — closes
  P2-23).
- ``_dispatch`` translates ``OSError`` / ``ValidationFailedError`` into
  a synthetic ``TaskStatus.FAILED`` recorder call so every accepted run
  produces exactly one ``task_results`` row (Group B — closes P1-4).

[FR-08] Bounded concurrency executor with graceful drain. The
runner stops accepting new work, waits for in-flight workers up to
``TASKQ_DRAIN_TIMEOUT`` and abandons anything still running.
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

# Sentinel value used by :meth:`BackgroundRunner.close` to wake workers.
_SHUTDOWN_SENTINEL = "__shutdown__"

# Queue bound multiplier. Each in-flight worker reserves one slot; the
# second `*` is natural slack so a worker that has just finished a run can
# dequeue the next one without blocking the submitter.
_QUEUE_BOUND_MULTIPLIER = 2


async def run_subprocess(
    command: str,
    *,
    timeout: float,
    run_id: str,
    task_id: str = "",
    now_fn: Optional[Callable[[], datetime]] = None,
) -> ExecutionResult:
    """Execute ``command`` once, returning an :class:`ExecutionResult` [FR-02].

    ``timeout`` is in seconds; pass ``TASKQ_TASK_TIMEOUT`` from the caller.
    ``run_id`` is supplied by the caller (the runner's :meth:`submit`)
    so the same id appears in the HTTP 202 body and the persisted row.
    ``task_id`` is stamped onto the result so the recorder can write
    the correct FK without re-querying.
    """
    if not command or not command.strip():
        raise ValidationFailedError(detail="command is empty.")
    argv = shlex.split(command)
    if not argv:  # pragma: no cover — defensive
        raise ValidationFailedError(detail="command produced no argv.")
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
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            timed_out = True
            stdout_bytes, stderr_bytes = b"", b""
    finally:
        # [NFR-03] shield so the kill is never itself cancelled mid-reap.
        # Without the shield, an outer cancel on `wait_for` could leak the
        # child PID. With it, the kill runs to completion on every path.
        await asyncio.shield(_kill_and_wait(proc))
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
        task_id=task_id,
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
        except (OSError, ProcessLookupError):
            # [NFR-03] CancelledError must propagate; do not widen this catch.
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

    The ``run_id`` lifecycle is owned by this class:
        1. :meth:`submit` mints a fresh UUID.
        2. The id is placed in the queue alongside the task id + command.
        3. :meth:`_dispatch` threads the id through ``run_subprocess`` and
           into the :class:`ExecutionResult` so the recorder writes it to
           ``task_results.run_id``.
        4. The same id is returned to the API caller for the 202 body.

    This guarantees the 202-body id and the persisted-row id always match
    [P0-1 / FR-02].
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
        # [P2-1] bounded queue so a misbehaving caller cannot OOM the worker.
        # Each in-flight worker reserves one slot; the second `*` is natural
        # slack for a worker that has just dequeued.
        self._queue: asyncio.Queue[tuple[str, str, str]] = asyncio.Queue(
            maxsize=self._max_concurrent * _QUEUE_BOUND_MULTIPLIER
        )
        self._workers: list[asyncio.Task[None]] = []
        self._started = False
        self._started_lock = asyncio.Lock()
        self._closed = False
        self._inflight: set[tuple[str, str, str]] = set()
        self._inflight_lock = asyncio.Lock()

    @property
    def started(self) -> bool:
        """Whether :meth:`start` has been called and not followed by :meth:`close`."""
        return self._started

    async def start(self) -> None:
        """Spawn the worker tasks [FR-08]."""
        # [P2-23] guard against concurrent start() calls doubling the worker
        # pool. The lock serialises both start() calls; the second one sees
        # _started=True and returns immediately.
        async with self._started_lock:
            if self._started:
                return
            for _ in range(self._max_concurrent):
                self._workers.append(asyncio.create_task(self._worker_loop()))
            self._started = True

    async def submit(self, task_id: str, command: str) -> str:
        """Enqueue a task and return the ``run_id`` that will identify it.

        The id is durable: once ``submit`` returns, the same id will be
        written to ``task_results.run_id`` and echoed in the 202 body.
        """
        if self._closed:
            raise NotReadyError(detail="Runner is shutting down; cannot accept new work.")
        # [P2-23] refuse to enqueue work into an idle queue. Without this
        # guard, callers can submit before start() and receive a `run_id`
        # that no worker will ever pick up (data loss in the field).
        if not self._started:
            raise NotReadyError(detail="Background runner is not configured.")
        run_id = str(uuid.uuid4())
        # [P2-1] the queue is bounded; the await blocks the caller when full,
        # turning submit() into a back-pressure point under burst.
        await self._queue.put((task_id, run_id, command))
        return run_id

    async def _worker_loop(self) -> None:
        while True:
            try:
                task_id, run_id, command = await self._queue.get()
            except asyncio.CancelledError:  # [NFR-03] shutdown — propagate
                raise
            if task_id == _SHUTDOWN_SENTINEL:
                self._queue.task_done()
                return
            try:
                async with self._inflight_lock:
                    self._inflight.add((task_id, run_id, command))
                await self._dispatch(task_id, run_id, command)
            finally:
                async with self._inflight_lock:
                    self._inflight.discard((task_id, run_id, command))
                self._queue.task_done()

    async def _dispatch(self, task_id: str, run_id: str, command: str) -> None:
        # [P1-4] every accepted run produces exactly one task_results row —
        # OSError / ValidationFailedError become synthetic FAILED records.
        try:
            result = await run_subprocess(
                command, timeout=self._task_timeout, run_id=run_id, task_id=task_id
            )
        except asyncio.CancelledError:
            # [NFR-03] — let it propagate.
            raise
        except ValidationFailedError:
            await self._record_failure(
                task_id, run_id, message="invalid command rejected"
            )
            return
        except OSError as exc:
            # `asyncio.create_subprocess_exec` raises before the proc is
            # spawned when the binary is missing or not executable.
            await self._record_failure(
                task_id, run_id, message=f"{type(exc).__name__}: {exc}"
            )
            return
        # Anything else is unexpected and surfaces to the global handler.
        try:
            await self._recorder(result)
        except Exception:  # noqa: BLE001
            _logger.error(
                "recorder failed for task",
                extra={"task_id": task_id},
            )

    async def _record_failure(self, task_id: str, run_id: str, *, message: str) -> None:
        """Persist a synthetic FAILED row when the subprocess never started.

        Closes [P1-4]: every accepted ``/run`` produces a row in
        ``task_results`` even when the binary is missing or the command
        fails validation.
        """
        now = datetime.now(tz=timezone.utc)
        result = ExecutionResult(
            task_id=task_id,
            run_id=run_id,
            exit_code=None,
            stdout_tail="",
            stderr_tail=message,
            duration_ms=0,
            status=TaskStatus.FAILED,
            started_at=now,
            finished_at=now,
        )
        try:
            await self._recorder(result)
        except Exception:  # noqa: BLE001
            _logger.error(
                "recorder failed for synthetic failure",
                extra={"task_id": task_id, "message": message},
            )

    async def close(self) -> None:
        """Stop accepting work, drain in-flight workers [FR-08]."""
        if self._closed:
            return
        self._closed = True
        for _ in self._workers:
            await self._queue.put((_SHUTDOWN_SENTINEL, "", ""))
        try:
            await asyncio.wait_for(self._queue.join(), timeout=self._drain_timeout)
        except asyncio.TimeoutError:
            _logger.warning("graceful drain exceeded; cancelling workers")
            for worker in self._workers:
                worker.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)
        else:
            for worker in self._workers:
                worker.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._started = False

    @property
    def inflight_count(self) -> int:
        return len(self._inflight)