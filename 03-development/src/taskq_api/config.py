"""Independence module — environment-driven configuration.

Loads the 12 TASKQ_* env vars (SPEC §5.1). Treated as independence in the
import-linter contract [NFR-06] so every other layer may import it without
violating layering rules.

Public surface
--------------
- :func:`get_settings` returns a cached :class:`Settings` instance.
- :class:`Settings` exposes the 12 taskq_* fields plus derived helpers.

Security
--------
- The raw ``taskq_db_url`` is *never* exposed on log/print paths; the
  password fragment is stripped in :meth:`Settings.safe_db_url`
  [NFR-04].
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import List, Optional


# [FR-06] Defaults mirror SPEC §5.1 exactly so the codebase is reproducible
# without a .env file.
DEFAULTS: dict[str, object] = {
    "taskq_db_url": "sqlite:///./taskq.db",
    "taskq_db_pool_size": 5,
    "taskq_task_timeout": 10.0,
    "taskq_max_concurrent": 8,
    "taskq_drain_timeout": 30.0,
    "taskq_rate_burst": 20,
    "taskq_rate_per_sec": 5.0,
    "taskq_cors_origins": "",
    "taskq_log_level": "INFO",
    "taskq_log_format": "json",
    "taskq_host": "127.0.0.1",
    "taskq_port": 8000,
}


def _coerce(name: str, raw: str) -> object:
    """Coerce a raw env string to the declared Python type for ``name``.

    [NFR-09] — anything ambiguous raises ``ValueError``; we never silently
    coerce to a wrong default.
    """
    default = DEFAULTS[name]
    if isinstance(default, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


def _env_name(name: str) -> str:
    """Map a DEFAULTS key (``taskq_log_level``) to its env var (``TASKQ_LOG_LEVEL``)."""
    return name.upper()


@dataclass(frozen=True)
class Settings:
    """Frozen view over the 12 taskq_* env vars.

    [FR-05] / [FR-06] / [FR-08] / [NFR-02] — every field is read once and
    cached; downstream code reads from this object instead of ``os.environ``.
    """

    taskq_db_url: str
    taskq_db_pool_size: int
    taskq_task_timeout: float
    taskq_max_concurrent: int
    taskq_drain_timeout: float
    taskq_rate_burst: int
    taskq_rate_per_sec: float
    taskq_cors_origins: str
    taskq_log_level: str
    taskq_log_format: str
    taskq_host: str
    taskq_port: int

    def cors_origins_list(self) -> List[str]:
        """Return the CORS allowlist [NFR-02].

        Empty list means deny-all.
        """
        return [o.strip() for o in self.taskq_cors_origins.split(",") if o.strip()]

    def safe_db_url(self) -> str:
        """Return ``taskq_db_url`` with any password fragment redacted.

        [NFR-04] — DB connection strings must never appear verbatim in logs,
        error bodies, or the ``/v1/metrics`` payload.
        """
        url = self.taskq_db_url
        if "@" not in url:
            return url
        scheme, rest = url.split("://", 1) if "://" in url else ("", url)
        if "@" not in rest:
            return url
        creds, host = rest.rsplit("@", 1)
        if ":" in creds:
            user, _ = creds.split(":", 1)
            return f"{scheme}://{user}:[REDACTED]@{host}"
        return f"{scheme}://[REDACTED]@{host}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings`.

    Values come from environment; unknown vars are ignored. Cache ensures
    one read per process so test fixtures can monkeypatch ``os.environ``
    before the first call [NFR-09].
    """
    values: dict[str, object] = {}
    for name in DEFAULTS:
        raw: Optional[str] = os.environ.get(_env_name(name))
        values[name] = _coerce(name, raw) if raw is not None else DEFAULTS[name]
    return Settings(**values)  # type: ignore[arg-type]


def reset_settings_cache() -> None:
    """Clear the cache — exposed for tests that mutate ``os.environ``."""
    get_settings.cache_clear()