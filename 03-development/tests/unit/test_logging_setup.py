"""[NFR-04] logging_setup — JSON formatting + redaction."""
from __future__ import annotations

import io
import json
import logging
import sys

import pytest

from taskq_api import logging_setup
from taskq_api.config import Settings


@pytest.fixture(autouse=True)
def _reset_logging() -> None:
    logging_setup._configured = False
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)


def _capture_output(formatter_json: bool) -> tuple[logging.Logger, io.StringIO]:
    """Install a fresh handler with our formatter and return the captured stream."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream=stream)
    handler.setFormatter(logging_setup._RedactingFormatter(json_format=formatter_json))
    logger = logging.getLogger(f"taskq_api.test_{id(stream)}")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger, stream


def test_json_format_includes_required_fields() -> None:
    logger, stream = _capture_output(True)
    logger.info("hello")
    payload = json.loads(stream.getvalue().splitlines()[0])
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["logger"].startswith("taskq_api")


def test_json_format_redacts_secret_message() -> None:
    logger, stream = _capture_output(True)
    logger.info("token=abcdefghijklmnop leaked")
    line = stream.getvalue().strip()
    assert "[REDACTED]" in line
    assert "abcdefghijklmnop" not in line


def test_text_format_redacts_secret() -> None:
    logger, stream = _capture_output(False)
    logger.info("Bearer abc123def456")
    out = stream.getvalue()
    assert "[REDACTED]" in out
    assert "abc123def456" not in out


def test_json_format_strips_reserved_keys() -> None:
    """[NFR-04] db_url/password/api_key never make it into the JSON log line."""
    logger, stream = _capture_output(True)
    logger.info("ready", extra={"db_url": "postgresql://x:y@h/d", "ok": True})
    payload = json.loads(stream.getvalue().strip())
    assert "db_url" not in payload
    assert payload.get("ok") is True


def test_configure_logging_idempotent() -> None:
    settings = Settings(
        taskq_db_url="sqlite:///./taskq.db",
        taskq_db_pool_size=1,
        taskq_task_timeout=1.0,
        taskq_max_concurrent=1,
        taskq_drain_timeout=1.0,
        taskq_rate_burst=1,
        taskq_rate_per_sec=1.0,
        taskq_cors_origins="",
        taskq_log_level="WARNING",
        taskq_log_format="text",
        taskq_host="127.0.0.1",
        taskq_port=8000,
    )
    logging_setup.configure_logging(settings)
    first_handlers = list(logging.getLogger().handlers)
    logging_setup.configure_logging(settings)
    second_handlers = list(logging.getLogger().handlers)
    assert first_handlers == second_handlers


def test_get_logger_namespaces() -> None:
    logger = logging_setup.get_logger("foo")
    assert logger.name == "taskq_api.foo"


def test_log_extra_drops_reserved_keys() -> None:
    filtered = logging_setup.log_extra({"db_url": "x", "task_id": "t1"})
    assert "db_url" not in filtered
    assert filtered["task_id"] == "t1"