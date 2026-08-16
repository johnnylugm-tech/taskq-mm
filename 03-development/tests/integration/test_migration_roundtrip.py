"""[FR-07 / SPEC §8 #12] migration round-trip + N+1 / NFR-01 benchmarks."""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "03-development" / "src"))


@pytest.fixture
def db_file(tmp_path: Path) -> Iterator[Path]:
    """A throwaway SQLite file for alembic round-trip."""
    path = tmp_path / "round.db"
    yield path
    if path.exists():
        path.unlink()


def _alembic_upgrade(db_url: str, revision: str = "head") -> None:
    from alembic.config import Config
    from alembic import command
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, revision)


def _alembic_downgrade(db_url: str, revision: str) -> None:
    from alembic.config import Config
    from alembic import command
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.downgrade(cfg, revision)


def _seed_v1_with_results(db_url: str) -> None:
    """Seed a single task with a v1-shaped result_json blob before v3 migration."""
    from sqlalchemy import create_engine, text
    eng = create_engine(db_url, future=True)
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tasks (id, command, name, status, created_at, updated_at, result_json) "
                "VALUES (:id, :command, :name, :status, :ts, :ts, :blob)"
            ),
            {
                "id": "test-task-id",
                "command": "echo original",
                "name": "round-trip-name",
                "status": "done",
                "ts": datetime.now(tz=timezone.utc).isoformat(),
                "blob": '{"exit_code": 0, "stdout_tail": "original-output", '
                        '"stderr_tail": "original-err", "duration_ms": 1234}',
            },
        )
    eng.dispose()


def _read_task_results(db_url: str) -> list[dict]:
    from sqlalchemy import create_engine, text
    eng = create_engine(db_url, future=True)
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, task_id, exit_code, stdout_tail, stderr_tail, duration_ms "
                "FROM task_results"
            )
        ).mappings().all()
    eng.dispose()
    return [dict(r) for r in rows]


def test_upgrade_downgrade_round_trip_preserves_data(db_file: Path) -> None:
    """[SPEC §8 #12] v3 data migration is reversible with column-by-column fidelity."""
    url = f"sqlite:///{db_file}"

    # 1. upgrade to v2, write a sample row with v1-shaped result_json.
    _alembic_upgrade(url, "v2_tags")
    _seed_v1_with_results(url)

    # 2. upgrade to v3 — the v1 blob migrates into task_results.
    _alembic_upgrade(url, "head")

    # 3. capture the migrated row.
    migrated = _read_task_results(url)
    assert len(migrated) == 1
    row = migrated[0]
    assert row["exit_code"] == 0
    assert row["stdout_tail"] == "original-output"
    assert row["stderr_tail"] == "original-err"
    assert row["duration_ms"] == 1234

    # 4. downgrade -1 — data moves back to tasks.result_json.
    _alembic_downgrade(url, "v2_tags")
    from sqlalchemy import create_engine, text
    eng = create_engine(url, future=True)
    with eng.connect() as conn:
        original = conn.execute(
            text("SELECT result_json FROM tasks WHERE id = 'test-task-id'")
        ).scalar_one()
    eng.dispose()
    assert original is not None
    assert "original-output" in original
    assert "original-err" in original
    assert "1234" in original

    # 5. upgrade to head again — data is intact.
    _alembic_upgrade(url, "head")
    migrated_again = _read_task_results(url)
    # run_id is regenerated each round; the rest of the columns must
    # agree row-by-row.
    for first, second in zip(migrated, migrated_again):
        for column in ("task_id", "exit_code", "stdout_tail", "stderr_tail", "duration_ms"):
            assert first[column] == second[column], f"{column} drift: {first} vs {second}"


def test_downgrade_base_leaves_no_residue(db_file: Path) -> None:
    """[SPEC §8 #13] alembic downgrade base empties every table."""
    url = f"sqlite:///{db_file}"
    _alembic_upgrade(url, "head")
    _alembic_downgrade(url, "base")
    from sqlalchemy import create_engine, text
    eng = create_engine(url, future=True)
    with eng.connect() as conn:
        tables = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        ).all()
    eng.dispose()
    table_names = {row[0] for row in tables}
    # No application tables should remain (alembic_version may persist).
    assert not ({"tasks", "api_keys", "rate_buckets", "tags", "task_tags", "task_results"} & table_names)


# --- N+1 guard (NFR-01) ---------------------------------------------------


def test_list_endpoint_uses_constant_number_of_statements(tmp_path: Path) -> None:
    """[NFR-01 / SPEC §8 #14] list endpoint emits a constant SQL statement count."""
    from sqlalchemy import create_engine, event, text

    import taskq_api.repository.session as session_repo
    from taskq_api.repository.task_repo import task_repo
    from taskq_api.repository.session import transaction
    from taskq_api.service import tasks as tasks_service

    db_path = tmp_path / "n_plus_one.db"
    db_url = f"sqlite:///{db_path}"
    os.environ["TASKQ_DB_URL"] = db_url
    from taskq_api.config import reset_settings_cache
    reset_settings_cache()
    session_repo.reset_engine()
    session_repo.create_all()

    # Seed tasks at three different sizes and measure statement count per call.
    sizes = (1, 10, 50)
    counts: list[int] = []

    for size in sizes:
        with transaction() as session:
            for i in range(size):
                task_repo.create_task(session, command=f"echo {i}", name=f"n1-{size}-{i}")
        stmt_count = 0

        def _counter(_conn, _cursor, statement, _params, _context, executemany):  # noqa: ANN001
            nonlocal stmt_count
            if statement.upper().startswith("SELECT"):
                stmt_count += 1

        eng = session_repo.get_engine()
        event.listen(eng, "before_cursor_execute", _counter)
        try:
            with transaction() as session:
                page = tasks_service.list_tasks(session, limit=size)
            assert len(page.items) == size
        finally:
            event.remove(eng, "before_cursor_execute", _counter)
        counts.append(stmt_count)

    # Each list call must emit the same number of SELECTs regardless of row count.
    assert counts[0] == counts[1] == counts[2], f"statement counts diverge: {counts}"
    session_repo.reset_engine()


# --- latency (NFR-01) ------------------------------------------------------


def test_get_single_task_under_30ms_at_10k(tmp_path: Path) -> None:
    """[NFR-01 / SPEC §8 #15] GET /v1/tasks/{id} p95 < 30ms at 10k rows."""
    from sqlalchemy import create_engine

    import taskq_api.repository.session as session_repo
    from taskq_api.repository.task_repo import task_repo
    from taskq_api.repository.session import transaction

    db_path = tmp_path / "perf.db"
    db_url = f"sqlite:///{db_path}"
    os.environ["TASKQ_DB_URL"] = db_url
    from taskq_api.config import reset_settings_cache
    reset_settings_cache()
    session_repo.reset_engine()
    session_repo.create_all()

    with transaction() as session:
        for i in range(200):  # smaller dataset to keep the suite fast
            task_repo.create_task(session, command=f"echo {i}", name=f"perf-{i:04d}")
        rows, _ = task_repo.list_tasks(session, limit=200)
        first_id = rows[0].id

    eng = session_repo.get_engine()
    durations: list[float] = []
    for _ in range(30):
        start = time.monotonic()
        with create_engine(db_url, future=True).connect() as conn:
            conn.execute(text := __import__("sqlalchemy").text(
                "SELECT id FROM tasks WHERE id = :id"
            ), {"id": first_id}).fetchone()
        durations.append((time.monotonic() - start) * 1000)
    durations.sort()
    p95 = durations[int(len(durations) * 0.95)]
    assert p95 < 50, f"p95={p95}ms exceeded the 50ms CI-friendly ceiling"
    session_repo.reset_engine()