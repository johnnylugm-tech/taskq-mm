"""Independence helper — JSON / text logging with redaction [NFR-04].

Public surface
--------------
- :func:`configure_logging` — install a single root handler honouring
  ``TASKQ_LOG_LEVEL`` and ``TASKQ_LOG_FORMAT``.
- :func:`get_logger` — convenience wrapper that always returns a named
  child logger with redaction applied to message rendering.
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any, Mapping, Optional

from .config import Settings, get_settings
from .redaction import redact_text

_LOGGER_NAME = "taskq_api"

# Anything in this attribute is itself redacted before formatting [NFR-04].
_RESERVED_LOG_KEYS = {"db_url", "password", "api_key", "key", "token"}


class _RedactingFormatter(logging.Formatter):
    """Formatter that redacts both the formatted message and the record's dict extras."""

    def __init__(self, *, json_format: bool) -> None:
        super().__init__()
        self._json = json_format

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401 — stdlib override
        if self._json:
            payload: dict[str, Any] = {
                "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
                "level": record.levelname,
                "logger": record.name,
                "message": redact_text(str(record.getMessage())),
            }
            for key, value in record.__dict__.items():
                if key in _RESERVED_LOG_KEYS or key.startswith("_"):
                    continue
                if isinstance(value, (str, int, float, bool)) or value is None:
                    if isinstance(value, str):
                        payload[key] = redact_text(value)
                    else:
                        payload[key] = value
            if record.exc_info:
                payload["exc_info"] = redact_text(self.formatException(record.exc_info))
            return json.dumps(payload, ensure_ascii=False)
        msg = redact_text(str(record.getMessage()))
        return f"{record.levelname} {record.name}: {msg}"


_configured = False


def configure_logging(settings: Optional[Settings] = None) -> None:
    """Install the project's log handler exactly once per process [NFR-04]."""
    global _configured
    if _configured:
        return
    resolved = settings if settings is not None else get_settings()
    root = logging.getLogger()
    json_format = resolved.taskq_log_format == "json"
    level = resolved.taskq_log_level.upper()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(_RedactingFormatter(json_format=json_format))
    root.addHandler(handler)
    root.setLevel(level)
    _configured = True


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a logger under the ``taskq_api`` namespace [NFR-04]."""
    return logging.getLogger(f"{_LOGGER_NAME}.{name}" if name else _LOGGER_NAME)


def log_extra(extra: Mapping[str, Any]) -> dict[str, Any]:
    """Drop any reserved keys from a dict before passing to ``Logger`` calls [NFR-04]."""
    return {k: v for k, v in extra.items() if k not in _RESERVED_LOG_KEYS}