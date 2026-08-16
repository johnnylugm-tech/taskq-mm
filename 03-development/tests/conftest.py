"""Shared pytest fixtures.

Every fixture here is module-scoped to keep test setup cheap. The DB is a
fresh SQLite file per session so the migration round-trip acceptance test
(SPEC §8 #12) can run against a real database.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Iterator

import pytest

# Make the source layout importable.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "03-development" / "src"))

# Ensure the project venv-installed packages are usable even when pytest
# is launched from the system interpreter.
VENV_SITE = ROOT / ".venv" / "lib" / "python3.11" / "site-packages"
if VENV_SITE.exists() and str(VENV_SITE) not in sys.path:
    sys.path.insert(0, str(VENV_SITE))


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Fresh SQLite file per test — sidesteps file-lock reuse between tests."""
    return tmp_path / "taskq.db"


@pytest.fixture(autouse=True)
def _env(db_path: Path) -> Iterator[None]:
    """Pin every TASKQ_* env var to test-friendly defaults.

    Captured & restored manually so per-test ``monkeypatch.setenv`` calls
    do not race with the fixture's own writes.
    """
    previous: dict[str, str | None] = {k: os.environ.get(k) for k in (
        "TASKQ_DB_URL", "TASKQ_DB_POOL_SIZE", "TASKQ_TASK_TIMEOUT",
        "TASKQ_MAX_CONCURRENT", "TASKQ_DRAIN_TIMEOUT", "TASKQ_RATE_BURST",
        "TASKQ_RATE_PER_SEC", "TASKQ_CORS_ORIGINS", "TASKQ_LOG_LEVEL",
        "TASKQ_LOG_FORMAT", "TASKQ_HOST", "TASKQ_PORT",
    )}
    os.environ["TASKQ_DB_URL"] = f"sqlite:///{db_path}"
    os.environ["TASKQ_DB_POOL_SIZE"] = "2"
    os.environ["TASKQ_TASK_TIMEOUT"] = "2.0"
    os.environ["TASKQ_MAX_CONCURRENT"] = "2"
    os.environ["TASKQ_DRAIN_TIMEOUT"] = "1.0"
    os.environ["TASKQ_RATE_BURST"] = "3"
    os.environ["TASKQ_RATE_PER_SEC"] = "1.0"
    os.environ["TASKQ_CORS_ORIGINS"] = ""
    os.environ["TASKQ_LOG_LEVEL"] = "WARNING"
    os.environ["TASKQ_LOG_FORMAT"] = "json"
    os.environ["TASKQ_HOST"] = "127.0.0.1"
    os.environ["TASKQ_PORT"] = "8000"

    from taskq_api.config import reset_settings_cache
    reset_settings_cache()
    try:
        yield
    finally:
        for key, prior in previous.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior
        reset_settings_cache()


@pytest.fixture(autouse=True)
def _reset_engine() -> Iterator[None]:
    """Reset the engine between tests so each test gets a fresh schema.

    Uses ``Base.metadata.create_all`` for speed; the round-trip alembic
    suite lives in the integration tests (SPEC §8 #12) and uses alembic
    directly on a real file [FR-07 / NFR-09].
    """
    import taskq_api.repository.session as session_repo
    from taskq_api.repository.session import create_all, drop_all, get_engine
    session_repo.reset_engine()
    create_all()
    yield
    try:
        drop_all(get_engine())
    except Exception:
        pass


@pytest.fixture
def api_key_seeded() -> dict[str, str]:
    """Create one key per scope and return ``{scope: plaintext}``."""
    from taskq_api.models.orm import Scope
    from taskq_api.repository.key_repo import key_repo
    from taskq_api.repository.session import transaction
    out: dict[str, str] = {}
    with transaction() as session:
        for scope in Scope:
            row, plaintext = key_repo.create_api_key(session, scope=scope)
            out[scope.value] = plaintext
    return out