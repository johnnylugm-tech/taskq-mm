# Project Brief — taskq-api

> **Round-2 brief.** This file lives in the `taskq-plus` spec library. When
> round 2 starts, copy it into its own repo as `PROJECT_BRIEF.md` alongside
> `SPEC-2.md` renamed to `SPEC.md` — the framework requires the canonical
> spec to be a bare `SPEC.md` at the project root.

## canonical_spec
SPEC.md (v1.0.0, 2026-07-30, 10 FR / **12 NFR** / 12 env vars)

## Project Domain
HTTP task-queue service: submit, query and execute shell-command tasks over
a REST API; persist to a relational database through SQLAlchemy; evolve the
schema with Alembic; authenticate with hashed API keys, authorise by scope,
and throttle per token.

## Stakeholders
- Project owner / product manager: johnnylugm-tech
- Integration test target: harness-methodology pipeline validation —
  **progressive test-bed round 2 of 3** (round 1 = `taskq-plus` CLI;
  round 3 = TypeScript, deferred)

## Business Goals
- Provide a task-queue HTTP service with authentication, authorisation,
  rate limiting, dependency-free horizontal scaling and a versioned schema
- Demonstrate the full Phase 1–8 harness-methodology pipeline on a layered
  web-service project (10 FR)
- **Exercise the axes neither previous test-bed could reach**: an HTTP
  boundary (authn/authz/input validation), a real database (ORM,
  transactions, N+1), real schema migration (Alembic with a data-moving
  step and a reversible downgrade), and async Python

## Why this project exists (test-bed intent)

Round 1 lit up `license_compliance`, `architecture_constraints`,
`mutation_testing` and `test_assertion_quality`, but it was still a
single-process CLI. These axes produced **no signal at all** in either
previous test-bed:

| Uncovered axis | This round's countermeasure | Clause |
|---|---|---|
| No HTTP layer → `security` only ever saw subprocess calls | REST API + API-key auth + per-token scope + rate limiting | FR-03/04/05, NFR-02 |
| No database → ORM, transactions, connection pools, N+1 all absent | SQLAlchemy ORM + explicit transaction boundaries + N+1 assertions | FR-06, NFR-01 |
| "Schema migration" was a hand-rolled JSON `version` field, and its tests were all skipped | **Alembic: three real revisions, one with data migration, every step reversible** | FR-07, NFR-03 |
| No async → the framework's scanners have never met `async def` | async endpoints + asyncio background runner | FR-08, NFR-03 |
| Shallow dependency tree | fastapi / sqlalchemy / alembic / uvicorn plus transitives, lock-file pinned | NFR-07 |
| Integration tests only ever drove a CLI subprocess | `httpx.ASGITransport` end-to-end incl. every error code | NFR-10 |

## Key Constraints
- **Technical**: Python 3.11; FastAPI ASGI app (`uvicorn taskq_api.app:app`);
  SQLAlchemy 2.x with explicit `Session` transaction boundaries; Alembic for
  migrations; `asyncio.create_subprocess_exec` for task execution —
  `shell=True` forbidden everywhere
- **Architecture**: four layers `api > service > repository > models`
  enforced by a mandatory `.importlinter` contract; `config` and `errors`
  are independence modules; **`sqlalchemy` may only be imported by
  `repository/`** — ORM leakage into the business layer is the specific
  anti-pattern this round guards against (NFR-06)
- **Security**: API keys stored as SHA-256 hashes and compared with
  `hmac.compare_digest`; 403 responses must not reveal whether the resource
  exists; no string-concatenated SQL anywhere; CORS denies all origins by
  default; error bodies must not carry stack traces, SQL or file paths
  (FR-03/04/10, NFR-02)
- **Migration**: three revisions — v1 base tables, v2 tags many-to-many,
  **v3 moves `tasks.result_json` into a `task_results` table with real data
  migration**; `upgrade head` → sample write → `downgrade -1` → `upgrade
  head` must leave every column byte-identical (FR-07)
- **Async correctness**: `asyncio.CancelledError` must propagate — it must
  never be swallowed by `except Exception`; task timeouts must actually kill
  the child process (`kill()` then `await wait()`), leaving no orphans;
  shutdown drains in-flight work up to `TASKQ_DRAIN_TIMEOUT` (FR-08, NFR-03)
- **Query efficiency**: relationship loads must be explicit
  (`selectinload` / `joinedload`); **N+1 is an acceptance failure** — the
  list endpoint's SQL statement count must be constant regardless of how
  many rows come back (NFR-01)
- **Readiness**: `/readyz` returns 503 when the database is unreachable
  **or** when `alembic current` is not at head — deploying new code without
  running the migration must fail closed (FR-09)
- **Verification honesty**: same zero-skip rule as round 1, plus a specific
  clause — the three-step migration must be tested against a **real
  database file**, not a mock, and may not be downgraded to a skip on the
  grounds that "migration logic is hard to test" (NFR-09)

## FR Inventory (canonical: SPEC.md §3)

| ID | Title | Section |
|----|-------|---------|
| FR-01 | 任務資源 CRUD API | POST/GET/LIST/DELETE `/v1/tasks`, cursor pagination, 422/404 |
| FR-02 | 任務執行端點 | `POST /v1/tasks/{id}/run` → 202; async subprocess; run history |
| FR-03 | API Key 認證 | `X-API-Key`, SHA-256 hashed, `hmac.compare_digest`, revocation |
| FR-04 | Scope 授權 | read < write < admin, single dependency, 403 leaks nothing |
| FR-05 | 流量控制 | per-token token bucket in DB, 429 + `Retry-After` |
| FR-06 | 持久化層與交易邊界 | repository layer, one Session per request, no raw SQL, no N+1 |
| FR-07 | Schema Migration | Alembic v1→v2→v3, v3 moves data, every step reversible |
| FR-08 | 非同步執行器 | `asyncio.TaskGroup`, concurrency cap, graceful drain, no orphans |
| FR-09 | 健康檢查與可觀測性 | `/healthz`, `/readyz` (fail-closed on migration lag), `/v1/metrics` |
| FR-10 | 錯誤契約 | RFC 7807 `application/problem+json` + `X-Correlation-Id` |

## NFR Inventory (canonical: SPEC.md §4)

> Every `dimension` below is a real key in `DIMENSION_TOOLS["python"]`.

| ID | dimension | Requirement |
|----|-----------|-------------|
| NFR-01 | `performance` | GET by id p95 < 30ms and list p95 < 80ms at 10k rows; **constant SQL statement count** (no N+1) |
| NFR-02 | `security` | no `shell=True`/`eval(`/`exec(`; no string-concatenated SQL; hashed keys + constant-time compare; 403 leaks nothing; CORS deny-by-default; bandit 0 HIGH / 0 MEDIUM |
| NFR-03 | `error_handling` | explicit transaction boundaries; no bare `except:`; **`CancelledError` must propagate**; timeouts kill children; failed migration rolls back |
| NFR-04 | `security` | redaction before write/emit, incl. the **database URL password** — never in logs, errors or metrics |
| NFR-05 | `documentation` | 100% public docstrings with `[FR-XX]`/`[NFR-XX]`; every endpoint documented in `/openapi.json` |
| NFR-06 | `architecture_constraints` | mandatory `.importlinter`: layers contract **plus** a forbidden contract banning `sqlalchemy` outside `repository/` |
| NFR-07 | `license_compliance` | `==` pinning + `requirements.lock` for transitives; allowlist; **scan the whole tree**; SBOM marks direct vs transitive |
| NFR-08 | `mutation_testing` | `features.mutation_testing: true`; score ≥ 70 over `service/` + `repository/` |
| NFR-09 | `test_assertion_quality` | **0 skipped**, 0 assertion-free tests, anti-fabrication clause, and migration must be tested against a real DB file |
| NFR-10 | `integration_coverage` | ≥ 80%, driven through `httpx.ASGITransport`, covering every error code |
| NFR-11 | `readability` | MI ≥ 80; CC ≤ 10; ≤ 400 lines/file; ≤ 15 files/dir; **≤ 40 lines per API handler** |
| NFR-12 | `execute_verification_target` | `make verify-system`: migrate → tests → health smoke → **migration round-trip**, exit 0 with `verify-system: PASS` |

## Env Var Inventory (canonical: SPEC.md §5.1 + .env.example)

| Variable | Default | Purpose |
|----------|---------|---------|
| `TASKQ_DB_URL` | `sqlite:///./taskq.db` | database connection string (**never logged** — NFR-04) |
| `TASKQ_DB_POOL_SIZE` | `5` | connection pool size (FR-06) |
| `TASKQ_TASK_TIMEOUT` | `10.0` | per-task subprocess timeout (seconds) |
| `TASKQ_MAX_CONCURRENT` | `8` | background execution concurrency cap (FR-08) |
| `TASKQ_DRAIN_TIMEOUT` | `30.0` | graceful drain budget on shutdown (FR-08) |
| `TASKQ_RATE_BURST` | `20` | token bucket capacity (FR-05) |
| `TASKQ_RATE_PER_SEC` | `5.0` | token refill rate (FR-05) |
| `TASKQ_CORS_ORIGINS` | (empty) | comma-separated allowlist; empty = deny all (NFR-02) |
| `TASKQ_LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR |
| `TASKQ_LOG_FORMAT` | `json` | json / text |
| `TASKQ_HOST` | `127.0.0.1` | bind address (not public by default) |
| `TASKQ_PORT` | `8000` | bind port |

## Database Schema (canonical: SPEC.md §5.2)

| Table | Revision | Key columns |
|-------|----------|-------------|
| `tasks` | v1 | `id` (uuid), `command`, `name`, `status`, `created_at` |
| `api_keys` | v1 | `id`, `key_hash` (sha256), `scope`, `created_at`, `revoked_at` |
| `rate_buckets` | v1 | `key_id` (FK), `tokens`, `updated_at` |
| `tags` | v2 | `id`, `label` |
| `task_tags` | v2 | `task_id`, `tag_id` (composite PK) |
| `task_results` | **v3** | `id`, `task_id` (FK), `exit_code`, `stdout_tail`, `stderr_tail`, `duration_ms`, `finished_at` |

`tasks.result_json` is created in v1 and removed in v3, its data migrated
into `task_results`. That step is the focus of the round-trip reversibility
acceptance test.

## Module Layout (canonical: SPEC.md §6)

```
03-development/src/taskq_api/
├── app.py                 # FastAPI assembly
├── config.py              # TASKQ_* env (independence)
├── errors.py              # RFC 7807 (independence, FR-10)
├── models/                # L1 — ORM tables + pydantic schemas
├── repository/            # L2 — the ONLY layer allowed to import sqlalchemy
│   ├── session.py         # Session + transaction context manager — high-risk
│   ├── task_repo.py
│   ├── key_repo.py
│   └── rate_repo.py
├── service/               # L3 — business logic, no ORM leakage
│   ├── tasks.py           # FR-01
│   ├── runner.py          # FR-02/08 async runner — high-risk
│   ├── auth.py            # FR-03/04 — high-risk
│   └── ratelimit.py       # FR-05
└── api/                   # L4 — FastAPI routes
    ├── deps.py            # single authn/authz decision point (FR-04)
    ├── tasks.py
    └── health.py

migrations/versions/
├── v1_initial.py
├── v2_tags.py
└── v3_split_results.py    # data migration + reversible downgrade — high-risk
```

Layering (enforced by `.importlinter`, NFR-06):
`api > service > repository > models`; `config` / `errors` independent;
`sqlalchemy` importable only from `repository/`.

## HTTP Status Map (canonical: SPEC.md §7)

| Status | Condition | `type` |
|--------|-----------|--------|
| 422 | request validation failed | `/errors/validation` |
| 401 | missing or invalid API key | `/errors/unauthenticated` |
| 403 | insufficient scope (leaks nothing) | `/errors/forbidden` |
| 404 | unknown task id | `/errors/not-found` |
| 409 | duplicate task name | `/errors/conflict` |
| 429 | rate limit exceeded (+ `Retry-After`) | `/errors/rate-limited` |
| 503 | DB down or migration behind head | `/errors/not-ready` |
| 500 | anything else (no stack/SQL/path in body) | `/errors/internal` |

`asyncio.CancelledError` is on none of these rows — it propagates (NFR-03).

## Acceptance Criteria (canonical: SPEC.md §8)

27 acceptance items, **each a single machine-decidable command with an
expected output**. Beyond the round-1 set, this round adds: every HTTP error
code exercised once; the migration round-trip with column-by-column data
comparison; `alembic downgrade base` leaving no tables; a constant SQL
statement count on the list endpoint; latency budgets at 10k rows; a scan
for string-concatenated SQL; proof that `api_keys` holds no plaintext; proof
that a 500 body carries no internals; proof that the DB URL password appears
in no log or metric; the `sqlalchemy` import ban enforced by `lint-imports`;
and graceful drain leaving no orphan processes.

## Risk Matrix (canonical: SPEC.md §9)

| ID | Risk | Mitigation |
|----|------|-----------|
| R1 | **v3 data migration loses data** | round-trip test against a real DB, column-by-column (FR-07) |
| R2 | SQL injection | no concatenation + ORM/parameterised + grep gate (NFR-02) |
| R3 | API key leak | hashed storage + constant-time compare + printed once (FR-03) |
| R4 | 403 reveals resource existence | authorise before lookup (FR-04) |
| R5 | N+1 collapses on a large table | explicit eager loading + SQL count assertion (NFR-01) |
| R6 | error body leaks internals | fixed RFC 7807 fields + detail allowlist (FR-10) |
| R7 | **swallowed `CancelledError` hangs shutdown** | explicit ban + assertion (NFR-03) |
| R8 | timeout leaves orphan processes | `kill()` + `await wait()` (FR-08) |
| R9 | deploy without migration | `/readyz` fails closed (FR-09) |
| R10 | connection pool exhaustion | `pool_pre_ping` + concurrency cap (FR-06/08) |
| R11 | transitive dep with incompatible license | lock file + whole-tree scan (NFR-07) |
| R12 | rate bucket race over-admits | single transaction + row-level lock (FR-05) |

## Source of Truth

All requirements are fully specified in `SPEC.md` (v1.0.0, 2026-07-30, this
round's copy of `SPEC-2.md`) at the project root — including the §10
framework alignment table and §11 monitoring thresholds.

Phase 1 workflow rules:
- Agent A must operate in INGESTION MODE: transcribe 100% of
  `### FR-01..FR-10` and `### NFR-01..NFR-12` headings — no invention,
  no omission.
- TBD / TODO / `<placeholder>` markers must be captured as `NFR-99` or
  `FR-XX-deferred`, never silently dropped.
- High-risk modules requiring per-module TDD coverage:
  `taskq_api.service.runner`, `taskq_api.service.auth`,
  `taskq_api.repository.session`, and
  `migrations/versions/v3_split_results.py`.
- **async is this round's new variable**: the framework's
  `ast-error-handling` and `ast-assertions` scanners have only ever faced
  synchronous code. Any misjudgement they make on `async def` is itself a
  finding this test-bed is meant to surface — record it in the Phase 4 bug
  hunt rather than working around it silently.
- The §5.3 project-side config files (`.importlinter`, `requirements.txt`,
  `requirements.lock`, `alembic.ini`, `.env.example`, `harness_config.json`,
  `Makefile`) are **not optional** — they carry NFR-06 / NFR-07 / NFR-08 /
  NFR-12 and FR-07, and their absence silently turns those dimensions back
  into free points.
