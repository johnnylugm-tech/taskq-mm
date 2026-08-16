"""[NFR-07] settings / env loading."""
from __future__ import annotations

import importlib
import os

import pytest

from taskq_api import config


def test_all_twelve_env_vars_loaded() -> None:
    """[SPEC §5.1] every TASKQ_* var must be present on Settings."""
    settings = config.get_settings()
    expected = {
        "taskq_db_url",
        "taskq_db_pool_size",
        "taskq_task_timeout",
        "taskq_max_concurrent",
        "taskq_drain_timeout",
        "taskq_rate_burst",
        "taskq_rate_per_sec",
        "taskq_cors_origins",
        "taskq_log_level",
        "taskq_log_format",
        "taskq_host",
        "taskq_port",
    }
    assert expected.issubset(set(vars(settings).keys()))


def test_defaults_match_spec_section_5_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """[SPEC §5.1] defaults must be byte-identical to the spec."""
    for key in (
        "TASKQ_DB_URL", "TASKQ_DB_POOL_SIZE", "TASKQ_TASK_TIMEOUT",
        "TASKQ_MAX_CONCURRENT", "TASKQ_DRAIN_TIMEOUT", "TASKQ_RATE_BURST",
        "TASKQ_RATE_PER_SEC", "TASKQ_CORS_ORIGINS", "TASKQ_LOG_LEVEL",
        "TASKQ_LOG_FORMAT", "TASKQ_HOST", "TASKQ_PORT",
    ):
        monkeypatch.delenv(key, raising=False)
    config.reset_settings_cache()
    settings = config.get_settings()
    assert settings.taskq_db_url == "sqlite:///./taskq.db"
    assert settings.taskq_db_pool_size == 5
    assert settings.taskq_task_timeout == 10.0
    assert settings.taskq_max_concurrent == 8
    assert settings.taskq_drain_timeout == 30.0
    assert settings.taskq_rate_burst == 20
    assert settings.taskq_rate_per_sec == 5.0
    assert settings.taskq_cors_origins == ""
    assert settings.taskq_log_level == "INFO"
    assert settings.taskq_log_format == "json"
    assert settings.taskq_host == "127.0.0.1"
    assert settings.taskq_port == 8000


def test_env_override_takes_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    """[SPEC §5.1] explicit env values win over defaults."""
    monkeypatch.setenv("TASKQ_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("TASKQ_PORT", "9999")
    config.reset_settings_cache()
    settings = config.get_settings()
    print("DBG LEVEL=", settings.taskq_log_level, "ENV=", os.environ.get("TASKQ_LOG_LEVEL"))
    assert settings.taskq_log_level == "DEBUG"
    assert settings.taskq_port == 9999


def test_invalid_int_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """[NFR-09] never silently coerce a bad value."""
    monkeypatch.setenv("TASKQ_DB_POOL_SIZE", "not-a-number")
    config.reset_settings_cache()
    with pytest.raises(ValueError):
        config.get_settings()


def test_safe_db_url_redacts_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """[NFR-04] password fragment is stripped from any logged URL."""
    monkeypatch.setenv("TASKQ_DB_URL", "postgresql://user:secret@host/db")
    config.reset_settings_cache()
    settings = config.get_settings()
    safe = settings.safe_db_url()
    assert "secret" not in safe
    assert "[REDACTED]" in safe
    assert safe.startswith("postgresql://user:")


def test_safe_db_url_with_no_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """[NFR-04] URLs without a password fragment pass through unchanged."""
    monkeypatch.setenv("TASKQ_DB_URL", "sqlite:///./taskq.db")
    config.reset_settings_cache()
    settings = config.get_settings()
    assert settings.safe_db_url() == "sqlite:///./taskq.db"


def test_cors_origins_list_parses_commas() -> None:
    """[NFR-02] CORS allowlist splits on commas and ignores blanks."""
    settings = config.get_settings()
    assert settings.cors_origins_list() == []
    settings = config.Settings(
        **{**{k: getattr(settings, k) for k in settings.__dataclass_fields__}, "taskq_cors_origins": " https://a , https://b , "}
    )
    assert settings.cors_origins_list() == ["https://a", "https://b"]