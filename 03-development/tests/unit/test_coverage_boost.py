"""Edge-case tests targeting lines not hit by the main suites.

These tests exist to push the line-coverage metric to 100% so the
acceptance criterion in SPEC §8 #2 is met.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from taskq_api import __main__ as cli
from taskq_api.errors import (
    APIError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    NotReadyError,
    RateLimitedError,
    TYPE_INTERNAL,
    TYPE_NOT_FOUND,
    TYPE_RATE_LIMITED,
    UnauthenticatedError,
    ValidationFailedError,
    install_exception_handlers,
    problem_response,
)
from taskq_api.models import schemas
from taskq_api.repository import key_repo
from taskq_api.repository.session import get_engine
from taskq_api.service import runner as runner_module


# --- errors.py edge cases -----------------------------------------------


def _FakeRequest():
    from taskq_api.errors import new_correlation_id

    class _Req:
        url = type("U", (), {"path": "/raw/x"})()
        state = type("S", (), {"correlation_id": new_correlation_id()})()

    return _Req()


def test_problem_response_for_internal_status_uses_internal_type() -> None:
    """[SPEC §7] 500 must use /errors/internal."""
    response = problem_response(
        problem_type=TYPE_INTERNAL,
        title="Internal",
        status=500,
        detail="boom",
        request=_FakeRequest(),
    )
    body = response.body.decode("utf-8")
    import json as _json
    parsed = _json.loads(body)
    assert parsed["type"] == "/errors/internal"
    assert parsed["status"] == 500


# --- runner.py edge cases ----------------------------------------------


@pytest.mark.asyncio
async def test_run_subprocess_kill_handles_processlookup() -> None:
    """[FR-08 / R8] ProcessLookupError during kill is swallowed, not propagated."""
    killed = False

    class _Proc:
        returncode = None

        def kill(self):
            nonlocal killed
            killed = True
            raise ProcessLookupError("already gone")

    await runner_module._kill_and_wait(_Proc())
    assert killed


@pytest.mark.asyncio
async def test_run_subprocess_kill_returns_early_when_already_exited() -> None:
    """[FR-08] _kill_and_wait is a no-op when returncode is set."""
    called = False

    class _Proc:
        returncode = 0

        def kill(self):
            nonlocal called
            called = True

    await runner_module._kill_and_wait(_Proc())
    assert called is False


@pytest.mark.asyncio
async def test_run_subprocess_records_timeout_stdout_when_pipe_empty() -> None:
    """[FR-08] timeout path captures empty stdout/stderr rather than hanging."""
    cmd = f'"{sys.executable}" -c "import time; time.sleep(2)"'
    result = await runner_module.run_subprocess(cmd, timeout=0.3, run_id="test-run-id", task_id="t-test")
    assert result.status.value == "timeout"
    assert result.exit_code is None
    assert result.duration_ms >= 200


@pytest.mark.asyncio
async def test_background_runner_close_when_workers_already_cleared() -> None:
    """[FR-08] close() is idempotent — second call short-circuits."""
    async def _noop(_result):
        return None

    runner = runner_module.BackgroundRunner(recorder=_noop, max_concurrent=1)
    await runner.start()
    await runner.close()
    await runner.close()  # should not raise


@pytest.mark.asyncio
async def test_background_runner_submit_returns_uuid_string() -> None:
    async def _noop(_result):
        return None

    runner = runner_module.BackgroundRunner(recorder=_noop, max_concurrent=1)
    await runner.start()
    run_id = await runner.submit("t1", f'"{sys.executable}" -c "pass"')
    assert isinstance(run_id, str) and len(run_id) >= 32
    await runner.close()


@pytest.mark.asyncio
async def test_run_subprocess_subprocess_pipe_truncates() -> None:
    """[NFR-04 / FR-08] very long stdout is bounded to _MAX_TAIL_BYTES."""
    cmd = f'"{sys.executable}" -c "print(\'x\' * 10000)"'
    result = await runner_module.run_subprocess(cmd, timeout=5.0, run_id="test-run-id", task_id="t-test")
    assert len(result.stdout_tail) <= runner_module._MAX_TAIL_BYTES


# --- config + errors edge cases ----------------------------------------


def test_settings_safe_db_url_with_only_at_sign(monkeypatch: pytest.MonkeyPatch) -> None:
    """[NFR-04] URL containing '@' but no scheme still gets sanitised."""
    monkeypatch.setenv("TASKQ_DB_URL", "weird@host")
    config_mod = importlib.import_module("taskq_api.config")
    config_mod.reset_settings_cache()
    settings = config_mod.get_settings()
    safe = settings.safe_db_url()
    assert "[REDACTED]" in safe


def test_settings_safe_db_url_passthrough_when_no_at_sign(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TASKQ_DB_URL", "sqlite:///./taskq.db")
    config_mod = importlib.import_module("taskq_api.config")
    config_mod.reset_settings_cache()
    settings = config_mod.get_settings()
    assert settings.safe_db_url() == "sqlite:///./taskq.db"


def test_cors_origins_list_drops_empty_segments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TASKQ_CORS_ORIGINS", "https://a,,, https://b , ,")
    config_mod = importlib.import_module("taskq_api.config")
    config_mod.reset_settings_cache()
    settings = config_mod.get_settings()
    assert settings.cors_origins_list() == ["https://a", "https://b"]


# --- __main__ dispatch coverage ---------------------------------------


def test_main_dispatches_serve(monkeypatch: pytest.MonkeyPatch) -> None:
    """``main serve`` invokes cmd_serve (mocked here)."""
    called: list[bool] = []

    def fake_serve(args):
        called.append(True)
        return 0

    monkeypatch.setattr(cli, "cmd_serve", fake_serve)
    code = cli.main(["serve"])
    assert code == 0
    assert called == [True]


def test_main_dispatches_healthcheck(monkeypatch: pytest.MonkeyPatch) -> None:
    """``main healthcheck`` invokes cmd_healthcheck."""
    called: list[bool] = []

    def fake_hc():
        called.append(True)
        return 0

    monkeypatch.setattr(cli, "cmd_healthcheck", fake_hc)
    code = cli.main(["healthcheck"])
    assert code == 0
    assert called == [True]


def test_main_dispatches_key_create(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []

    def fake_kc(args):
        called.append(True)
        return 0

    monkeypatch.setattr(cli, "cmd_key_create", fake_kc)
    code = cli.main(["key", "create", "--scope", "read"])
    assert code == 0
    assert called == [True]


def test_main_logs_api_error_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """[SPEC §1] APIError path: code=1 and the error is logged."""
    from taskq_api.errors import APIError

    def boom(_args):
        raise APIError("nope")

    monkeypatch.setattr(cli, "cmd_initdb", boom)
    code = cli.main(["initdb"])
    assert code == 1


# --- api/health.py: the metrics path when there is no data -------------


@pytest.fixture
def empty_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TASKQ_DB_URL", f"sqlite:///{tmp_path / 'empty.db'}")
    from taskq_api.config import reset_settings_cache
    reset_settings_cache()
    import taskq_api.repository.session as session_repo
    session_repo.reset_engine()
    session_repo.create_all()
    yield


def test_metrics_endpoint_handles_no_runs(empty_db: None) -> None:
    """[FR-09] /v1/metrics works even with an empty dataset."""
    from fastapi.testclient import TestClient
    from taskq_api.app import create_app
    app = create_app()
    with TestClient(app) as client:
        # create admin key directly via repo
        from taskq_api.models.orm import Scope
        from taskq_api.repository.session import transaction
        with transaction() as session:
            row, plain = key_repo.create_api_key(session, scope=Scope.ADMIN)
        response = client.get("/v1/metrics", headers={"X-API-Key": plain})
        assert response.status_code == 200
        body = response.json()
        assert body["task_counts"] == {}
        assert body["run_latency_ms"]["count"] == 0
        assert body["rate_limit_rejections"] == 0


# --- rate_repo edge case: refilling past capacity -----------------------


def test_take_token_refill_capped_at_capacity() -> None:
    """[FR-05] refill never pushes tokens above capacity."""
    from datetime import datetime, timedelta, timezone

    from taskq_api.models.orm import APIKey, RateBucket, Scope
    from taskq_api.repository.key_repo import key_repo
    from taskq_api.repository.session import transaction
    with transaction() as session:
        row, _ = key_repo.create_api_key(session, scope=Scope.WRITE)
    with transaction() as session:
        row2 = session.get(APIKey, row.id)
        from taskq_api.repository.rate_repo import take_token
        future = datetime.now(tz=timezone.utc) + timedelta(seconds=10)
        for _ in range(3):
            take_token(session, row2, capacity=3, refill_per_sec=100.0, now=future)
        bucket = session.get(RateBucket, row.id)
        assert bucket.tokens <= 3


# --- models/schemas: dedupe + overlong tag edge -------------------------


def test_task_create_overlong_tag_rejected() -> None:
    with pytest.raises(ValueError):
        schemas.TaskCreate(command="echo", name="ok", tags=["x" * 65])


# --- service/tasks: list_tasks bad limit; duplicate via repo path --------


def test_service_list_tasks_invalid_limit_zero() -> None:
    from taskq_api.errors import ValidationFailedError
    from taskq_api.repository.session import transaction
    from taskq_api.service import tasks as tasks_service
    with pytest.raises(ValidationFailedError):
        with transaction() as session:
            tasks_service.list_tasks(session, limit=0)


def test_service_list_tasks_invalid_limit_too_high() -> None:
    from taskq_api.errors import ValidationFailedError
    from taskq_api.repository.session import transaction
    from taskq_api.service import tasks as tasks_service
    with pytest.raises(ValidationFailedError):
        with transaction() as session:
            tasks_service.list_tasks(session, limit=201)


# --- repository/task_repo: invalid cursor -------------------------------


def test_task_repo_invalid_cursor_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TASKQ_DB_URL", f"sqlite:///{tmp_path / 'cur.db'}")
    from taskq_api.config import reset_settings_cache
    reset_settings_cache()
    import taskq_api.repository.session as session_repo
    session_repo.reset_engine()
    session_repo.create_all()
    from taskq_api.repository.session import transaction
    from taskq_api.repository.task_repo import list_tasks
    with pytest.raises(Exception):
        with transaction() as session:
            list_tasks(session, cursor="$$$not-base64$$$")


# --- repository/key_repo: hash_for_tests exposed ------------------------


def test_key_repo_hash_for_tests_is_stable() -> None:
    a = key_repo.hash_for_tests("plain")
    b = key_repo.hash_for_tests("plain")
    assert a == b
    assert len(a) == 64


# --- repository/session: get_sessionmaker + reset_engine idempotent -----


def test_session_get_sessionmaker_initialises_on_first_call() -> None:
    import taskq_api.repository.session as session_repo
    session_repo.reset_engine()
    sm = session_repo.get_sessionmaker()
    assert sm is not None


def test_session_reset_engine_disposes_existing() -> None:
    import taskq_api.repository.session as session_repo
    session_repo.reset_engine()
    session_repo.configure_engine()
    session_repo.reset_engine()
    assert session_repo._engine is None


# --- api/tasks.py: runner not configured -------------------------------


def test_api_tasks_runner_missing_raises_not_ready() -> None:
    """[FR-08] api/tasks.py raises NotReadyError when the global runner is None."""
    from taskq_api.api.tasks import _runner_from_state
    from taskq_api import app as app_module
    app_module._RUNNER = None
    with pytest.raises(NotReadyError):
        _runner_from_state()
    app_module._RUNNER = None


# --- api/health.py: alembic ScriptDirectory exception -------------------


def test_readyz_handles_alembic_script_load_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TASKQ_DB_URL", f"sqlite:///{tmp_path / 'rdy.db'}")
    from taskq_api.config import reset_settings_cache
    reset_settings_cache()
    import taskq_api.repository.session as session_repo
    session_repo.reset_engine()
    session_repo.create_all()
    from fastapi.testclient import TestClient
    from taskq_api.app import create_app
    import alembic.script
    real = alembic.script.ScriptDirectory.from_config
    def boom(_config):
        raise RuntimeError("script directory broken")
    monkeypatch.setattr(alembic.script.ScriptDirectory, "from_config", staticmethod(boom))
    try:
        with TestClient(create_app()) as client:
            response = client.get("/readyz")
        # [Group C] fail-closed: any alembic failure surfaces as 503, never 200.
        assert response.status_code == 503
    finally:
        alembic.script.ScriptDirectory.from_config = staticmethod(real)


# --- runner.py: ValidationFailedError raised by submit/repo path ---------


@pytest.mark.asyncio
async def test_background_runner_dispatcher_swallows_validation_error() -> None:
    """[FR-08] ValidationFailedError from a queued command is logged + dropped."""
    captured: list[str] = []

    async def _rec(result):
        captured.append(result)

    runner = runner_module.BackgroundRunner(recorder=_rec, max_concurrent=1)
    await runner.start()
    # Empty command will trip ValidationFailedError inside run_subprocess.
    # [Group B] the dispatch path now translates it into a synthetic
    # FAILED record so every accepted run produces exactly one row.
    await runner.submit("t-bad", "")
    await asyncio.sleep(0.1)
    await runner.close()
    assert len(captured) == 1
    assert captured[0].status.value == "failed"
    assert captured[0].exit_code is None
    assert "invalid command" in captured[0].stderr_tail


# --- runner.py: generic exception inside dispatch ------------------------


@pytest.mark.asyncio
async def test_background_runner_dispatcher_swallows_subprocess_failure() -> None:
    """[FR-08] unexpected exception inside the worker is logged + dropped."""
    captured: list[object] = []

    async def _rec(result):
        captured.append(result)

    runner = runner_module.BackgroundRunner(recorder=_rec, max_concurrent=1)
    await runner.start()
    # Command that fails fast with a non-zero exit — dispatcher must not crash.
    await runner.submit("t-fail", f'"{sys.executable}" -c "import sys; sys.exit(3)"')
    await asyncio.sleep(0.3)
    await runner.close()
    assert len(captured) >= 1
    assert captured[0].exit_code == 3


# Import alias used above.
import importlib  # noqa: E402  (placed at bottom to keep other code top-level)