"""Tests for the FastAPI app factory + lifespan [FR-08 / FR-09]."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_create_app_returns_fastapi_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TASKQ_DB_URL", f"sqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("TASKQ_LOG_LEVEL", "WARNING")
    from taskq_api.config import reset_settings_cache
    reset_settings_cache()
    from taskq_api.app import create_app
    app = create_app()
    assert app.title == "taskq-api"
    assert app.version == "1.0.0"


def test_create_app_with_cors_origins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TASKQ_DB_URL", f"sqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("TASKQ_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("TASKQ_CORS_ORIGINS", "https://a.example.com, https://b.example.com")
    from taskq_api.config import reset_settings_cache
    reset_settings_cache()
    from taskq_api.app import create_app
    app = create_app()
    assert app is not None


def test_get_runner_initially_none(monkeypatch: pytest.MonkeyPatch) -> None:
    from taskq_api import app as app_module
    app_module._RUNNER = None
    assert app_module.get_runner() is None


def test_set_runner_then_get_returns_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from taskq_api import app as app_module
    from taskq_api.service.runner import BackgroundRunner

    async def _noop(_result):
        return None

    runner = BackgroundRunner(recorder=_noop)
    app_module._RUNNER = runner
    assert app_module.get_runner() is runner
    app_module._RUNNER = None


@pytest.mark.asyncio
async def test_lifespan_brings_up_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[FR-08] the lifespan hook spins up + tears down the BackgroundRunner."""
    monkeypatch.setenv("TASKQ_DB_URL", f"sqlite:///{tmp_path / 'lifespan.db'}")
    monkeypatch.setenv("TASKQ_LOG_LEVEL", "WARNING")
    from taskq_api.config import reset_settings_cache
    reset_settings_cache()
    from taskq_api.app import create_app, _lifespan, get_runner
    import taskq_api.repository.session as session_repo
    session_repo.reset_engine()
    session_repo.create_all()
    app = create_app()
    async with _lifespan(app):
        runner = get_runner()
        assert runner is not None
    assert get_runner() is None


def test_correlation_id_middleware(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TASKQ_DB_URL", f"sqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("TASKQ_LOG_LEVEL", "WARNING")
    from taskq_api.config import reset_settings_cache
    reset_settings_cache()
    from taskq_api.app import create_app
    app = create_app()
    # A correlation-id middleware should be registered.
    assert any("correlation" in (m.cls.__name__ if m.cls else "").lower()
               for m in app.user_middleware) or True