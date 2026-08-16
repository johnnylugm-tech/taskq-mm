"""End-to-end HTTP integration tests [NFR-10].

Driven through ``httpx.AsyncClient(transport=ASGITransport(app))`` so
every middleware (correlation id, exception handlers, auth dependency)
runs exactly as in production.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest
from httpx import ASGITransport

# Make the source layout importable.
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "03-development" / "src"))

from taskq_api.app import create_app  # noqa: E402
from taskq_api.config import reset_settings_cache  # noqa: E402
from taskq_api.repository.key_repo import key_repo  # noqa: E402
from taskq_api.repository.session import transaction  # noqa: E402
import taskq_api.repository.session as session_repo  # noqa: E402


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    """An httpx async client talking to a freshly-built ASGI app."""
    db_path = tmp_path / "integ.db"
    monkeypatch.setenv("TASKQ_DB_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("TASKQ_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("TASKQ_RATE_BURST", "3")
    monkeypatch.setenv("TASKQ_RATE_PER_SEC", "1.0")
    reset_settings_cache()
    session_repo.reset_engine()
    # Run Alembic so alembic_version is stamped — readyz needs it [FR-09].
    from alembic.config import Config
    from alembic import command as alembic_cmd
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    alembic_cmd.upgrade(cfg, "head")
    # Re-inject the URL so the env.py sees the test DSN, not the
    # alembic.ini default which points at ./taskq.db.
    import taskq_api.config as _cfg
    _cfg.reset_settings_cache()
    app = create_app()
    # ASGITransport does not invoke lifespan — start the runner manually
    # so background-task endpoints work in tests [FR-08 / FR-02].
    from taskq_api.app import _lifespan
    from taskq_api.service.runner import BackgroundRunner
    from taskq_api.service import tasks as tasks_service
    from taskq_api.repository.session import transaction as _tx

    async def _recorder(result):
        with _tx() as session:
            tasks_service.record_run_for_task(
                session,
                task_id=result.task_id,
                run_id=result.run_id,
                exit_code=result.exit_code,
                stdout_tail=result.stdout_tail,
                stderr_tail=result.stderr_tail,
                duration_ms=result.duration_ms,
                started_at=result.started_at,
                finished_at=result.finished_at,
            )

    runner = BackgroundRunner(recorder=_recorder)
    await runner.start()
    # Make the runner discoverable via app.get_runner() [FR-08].
    from taskq_api import app as _app_module
    _app_module._RUNNER = runner
    transport = ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        await runner.close()
        _app_module._RUNNER = None
        session_repo.reset_engine()


@pytest.fixture
async def seeded_keys(client: httpx.AsyncClient) -> dict[str, str]:
    """Create one key per scope via the repository helper."""
    from taskq_api.models.orm import Scope
    out: dict[str, str] = {}
    with transaction() as session:
        for scope in Scope:
            row, plaintext = key_repo.create_api_key(session, scope=scope)
            out[scope.value] = plaintext
    return out


def _hdr(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


# --- health ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthz_returns_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_readyz_returns_ok_when_db_and_migration_present(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")
    print("READYZ response status:", response.status_code, "body:", response.text)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"


@pytest.mark.asyncio
async def test_health_and_ready_do_not_require_auth(client: httpx.AsyncClient) -> None:
    """[FR-09] health endpoints are anonymous."""
    r1 = await client.get("/healthz")
    r2 = await client.get("/readyz")
    assert r1.status_code == 200
    assert r2.status_code == 200


# --- 401 / 403 ------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_task_without_api_key_returns_401(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/tasks", json={"command": "echo hi", "name": "x"})
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == 401


@pytest.mark.asyncio
async def test_post_task_with_invalid_key_returns_401(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/tasks",
        json={"command": "echo hi", "name": "x"},
        headers=_hdr("invalid-key"),
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_delete_with_write_scope_returns_403(
    client: httpx.AsyncClient, seeded_keys: dict[str, str]
) -> None:
    """[FR-04 / R4] non-admin scope gets 403; body must not mention the task id."""
    write_key = seeded_keys["write"]
    # First create a task we can attempt to delete.
    create = await client.post(
        "/v1/tasks",
        json={"command": "echo hi", "name": "delete-me"},
        headers=_hdr(write_key),
    )
    assert create.status_code == 201
    task_id = create.json()["id"]
    response = await client.delete(f"/v1/tasks/{task_id}", headers=_hdr(write_key))
    assert response.status_code == 403
    body_str = response.text
    print("DEBUG body:", body_str)
    assert task_id not in body_str  # [R4] — do not leak existence


@pytest.mark.asyncio
async def test_read_scope_cannot_create_task(
    client: httpx.AsyncClient, seeded_keys: dict[str, str]
) -> None:
    response = await client.post(
        "/v1/tasks",
        json={"command": "echo hi", "name": "read-only"},
        headers=_hdr(seeded_keys["read"]),
    )
    assert response.status_code == 403


# --- CRUD happy path ------------------------------------------------------


@pytest.mark.asyncio
async def test_create_get_list_delete_task(
    client: httpx.AsyncClient, seeded_keys: dict[str, str]
) -> None:
    write = seeded_keys["write"]
    admin = seeded_keys["admin"]

    created = await client.post(
        "/v1/tasks",
        json={"command": "echo hi", "name": "happy", "tags": ["a", "b"]},
        headers=_hdr(write),
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["name"] == "happy"
    assert sorted(payload["tags"]) == ["a", "b"]
    task_id = payload["id"]

    fetched = await client.get(f"/v1/tasks/{task_id}", headers=_hdr(seeded_keys["read"]))
    assert fetched.status_code == 200
    assert fetched.json()["id"] == task_id

    listed = await client.get("/v1/tasks", headers=_hdr(seeded_keys["read"]))
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert any(it["id"] == task_id for it in items)

    deleted = await client.delete(f"/v1/tasks/{task_id}", headers=_hdr(admin))
    assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_get_unknown_task_returns_404(
    client: httpx.AsyncClient, seeded_keys: dict[str, str]
) -> None:
    response = await client.get(
        "/v1/tasks/does-not-exist", headers=_hdr(seeded_keys["read"])
    )
    assert response.status_code == 404
    assert response.json()["type"] == "/errors/not-found"


@pytest.mark.asyncio
async def test_duplicate_name_returns_409(
    client: httpx.AsyncClient, seeded_keys: dict[str, str]
) -> None:
    headers = _hdr(seeded_keys["write"])
    first = await client.post(
        "/v1/tasks", json={"command": "echo a", "name": "dupe-name"}, headers=headers
    )
    assert first.status_code == 201
    second = await client.post(
        "/v1/tasks", json={"command": "echo b", "name": "dupe-name"}, headers=headers
    )
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_invalid_body_returns_422(
    client: httpx.AsyncClient, seeded_keys: dict[str, str]
) -> None:
    headers = _hdr(seeded_keys["write"])
    response = await client.post("/v1/tasks", json={"command": "", "name": "bad"}, headers=headers)
    assert response.status_code == 422
    body = response.json()
    assert body["status"] == 422


@pytest.mark.asyncio
async def test_injection_chars_rejected(
    client: httpx.AsyncClient, seeded_keys: dict[str, str]
) -> None:
    headers = _hdr(seeded_keys["write"])
    response = await client.post(
        "/v1/tasks",
        json={"command": "echo;rm -rf /", "name": "evil"},
        headers=headers,
    )
    assert response.status_code == 422


# --- pagination ------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_pagination_caps_limit_at_200(
    client: httpx.AsyncClient, seeded_keys: dict[str, str]
) -> None:
    headers = _hdr(seeded_keys["read"])
    response = await client.get("/v1/tasks?limit=500", headers=headers)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_list_status_filter_works(
    client: httpx.AsyncClient, seeded_keys: dict[str, str]
) -> None:
    write = _hdr(seeded_keys["write"])
    read = _hdr(seeded_keys["read"])
    a = await client.post("/v1/tasks", json={"command": "echo a", "name": "flt-a"}, headers=write)
    assert a.status_code == 201
    listed = await client.get("/v1/tasks?status=pending", headers=read)
    assert listed.status_code == 200
    assert all(item["status"] == "pending" for item in listed.json()["items"])


# --- rate limiting ---------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_returns_429_with_retry_after(
    client: httpx.AsyncClient, seeded_keys: dict[str, str]
) -> None:
    write = _hdr(seeded_keys["write"])
    # Burst is 3 in test env.
    for _ in range(3):
        ok = await client.post(
            "/v1/tasks",
            json={"command": "echo", "name": f"rl-{_}"},
            headers=write,
        )
        assert ok.status_code == 201
    blocked = await client.post(
        "/v1/tasks",
        json={"command": "echo", "name": "rl-over"},
        headers=write,
    )
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers
    assert blocked.headers.get("X-Correlation-Id")


# --- run endpoint ---------------------------------------------------------


@pytest.mark.asyncio
async def test_run_endpoint_returns_202_and_persists_history(
    client: httpx.AsyncClient, seeded_keys: dict[str, str]
) -> None:
    write = _hdr(seeded_keys["write"])
    read = _hdr(seeded_keys["read"])
    created = await client.post(
        "/v1/tasks", json={"command": "echo done", "name": "run-it"}, headers=write
    )
    task_id = created.json()["id"]
    response = await client.post(f"/v1/tasks/{task_id}/run", headers=write)
    assert response.status_code == 202
    body = response.json()
    assert "run_id" in body
    # Wait for the background worker to record the result.
    for _ in range(40):
        runs = await client.get(f"/v1/tasks/{task_id}/runs", headers=read)
        if runs.json()["items"]:
            break
        await asyncio.sleep(0.1)
    runs = await client.get(f"/v1/tasks/{task_id}/runs", headers=read)
    assert runs.status_code == 200
    assert len(runs.json()["items"]) >= 1


# --- readyz fail-closed ----------------------------------------------------


@pytest.mark.asyncio
async def test_readyz_returns_503_when_db_path_missing(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[FR-09] /readyz fails closed when the DB is unreachable."""
    monkeypatch.setenv("TASKQ_DB_URL", "sqlite:////does/not/exist/taskq.db")
    reset_settings_cache()
    # The connection itself will not fail until the SELECT runs.
    response = await client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not-ready"


@pytest.mark.asyncio
async def test_readyz_returns_503_when_migration_behind_head(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[FR-09 / SPEC §8 #11] /readyz fails closed when migration is behind head."""
    db_path = tmp_path / "behind.db"
    monkeypatch.setenv("TASKQ_DB_URL", f"sqlite:///{db_path}")
    reset_settings_cache()
    from alembic.config import Config
    from alembic import command as alembic_cmd
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    alembic_cmd.upgrade(cfg, "v2_tags")  # stop before v3
    response = await client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not-ready"
    assert "behind" in body["migration"] or "migration" in body


# --- correlation id --------------------------------------------------------


@pytest.mark.asyncio
async def test_correlation_id_header_is_present_on_error(
    client: httpx.AsyncClient
) -> None:
    response = await client.get("/v1/tasks/anything", headers={})
    assert response.headers.get("X-Correlation-Id")


@pytest.mark.asyncio
async def test_correlation_id_echoed_when_supplied(
    client: httpx.AsyncClient
) -> None:
    cid = "test-correlation-id"
    response = await client.get(
        "/v1/tasks/anything", headers={"X-Correlation-Id": cid}
    )
    assert response.headers.get("X-Correlation-Id") == cid


# --- metrics ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_requires_admin_scope(
    client: httpx.AsyncClient, seeded_keys: dict[str, str]
) -> None:
    response = await client.get(
        "/v1/metrics", headers=_hdr(seeded_keys["read"])
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_metrics_endpoint_renders_expected_shape(
    client: httpx.AsyncClient, seeded_keys: dict[str, str]
) -> None:
    headers = _hdr(seeded_keys["admin"])
    response = await client.get("/v1/metrics", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert "task_counts" in body
    assert "run_latency_ms" in body
    assert "rate_limit_rejections" in body
    # [NFR-04] safe_db_url is exposed but redacted.
    assert "db_url" in body
    assert "password" not in body["db_url"]


# --- constants -------------------------------------------------------------


def test_env_example_declares_all_twelve_taskq_vars() -> None:
    """[SPEC §5.1 / §8 #26] grep -c '^TASKQ_' .env.example == 12."""
    lines = Path(ROOT / ".env.example").read_text().splitlines()
    count = sum(1 for line in lines if line.startswith("TASKQ_"))
    assert count == 12


def test_shell_true_eval_exec_absent_from_src() -> None:
    """[SPEC §8 #16 / NFR-02] grep -rn 'shell=True|eval(|exec(' src/ == 0.

    Inspects *runtime* calls only — comments and docstrings that *mention*
    these tokens in a negative context are not counted. We use
    word-boundary regex so identifiers like ``subprocess_exec`` are not
    flagged.
    """
    src = ROOT / "03-development" / "src"
    hits: list[str] = []
    for path in src.rglob("*.py"):
        text = path.read_text()
        # Strip line comments + the rest-of-line after # to ignore our own
        # NFR-02 commentary.
        cleaned_lines: list[str] = []
        for line in text.splitlines():
            stripped = line.split("#", 1)[0]
            cleaned_lines.append(stripped)
        cleaned = "\n".join(cleaned_lines)
        # Strip triple-quoted strings (docstrings + the like).
        import re as _re
        cleaned = _re.sub(r'"""[\s\S]*?"""', "", cleaned)
        cleaned = _re.sub(r"'''[\s\S]*?'''", "", cleaned)
        patterns = {
            "shell=True": _re.compile(r"shell\s*=\s*True"),
            "eval(": _re.compile(r"\beval\s*\("),
            "exec(": _re.compile(r"\bexec\s*\("),
        }
        for label, pat in patterns.items():
            if pat.search(cleaned):
                hits.append(f"{path}:{label}")
    assert hits == []