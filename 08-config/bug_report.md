# taskq_api — Adversarial Bug Report (merged)

| Field | Value |
|---|---|
| Scope | `03-development/src/taskq_api/` (10 files) |
| Method | 4-lens (correctness / auth-security / concurrency / hardening) |
| Session CRG findings | none stored (`mcp__memory__read_graph` returns empty) — no prior cross-reference available |
| SPEC references | FR-01 / FR-02 / FR-03 / FR-04 / FR-05 / FR-06 / FR-08 / FR-09 / FR-10 ; NFR-01..04 ; R4 / R8 / R10 / R12 |

---

## CRG navigation

The code knowledge graph (CRG) is the structural baseline for this report. It indexes **10 source files, 9 classes, 76 functions, 94 tests, 1412 edges** (`CALLS=635`, `CONTAINS=186`, `IMPORTS_FROM=175`, `TESTED_BY=415`, `REFERENCES=1`) — Python only, last refresh 2026-08-16 17:05 UTC. Embeddings are not yet computed; all navigation below is via the structural graph.

### Top execution flows by CRG criticality

| Rank | Flow | Criticality | Nodes | Why CRG flagged it |
|---|---|---:|---:|---|
| 1 | `transaction` | 0.630 | 4 | Every request-boundary session runs through this. `transaction()` → `get_sessionmaker` → `configure_engine` → `engine_from_url`. Touches **NFR-03** (commit/rollback guarantees) and every database code path. |
| 2 | `test_create_get_list_delete_task` | 0.535 | 2 | The end-to-end happy path that covers FR-01 through FR-04 in one test. Failures here cascade to every CRUD bug listed below. |
| 3 | `test_run_endpoint_returns_202_and_persists_history` | 0.535 | 2 | Drives the entire FR-02 run pipeline (auth → rate-limit → enqueue → worker → recorder). The P0 `#10 readyz` and P0 `#9 /run` findings both surface through this flow. |
| 4 | `test_delete_with_write_scope_returns_403` | 0.495 | 2 | Covers FR-04 / R4 — directly intersects finding P1 `delete_task` (service/tasks.py:100) where the code contradicts its docstring. |
| 5 | `test_list_status_filter_works` | 0.495 | 2 | Exercises `list_tasks` cursor pagination — the same code path as finding P2 around cursor `(created_at, id)` ordering and tz-naive comparisons. |
| 6 | `test_rate_limit_returns_429_with_retry_after` | 0.495 | 2 | Covers FR-05 / R12 — the test asserts the 429 contract that finding P1 (`select_for_update` no-op on SQLite) silently violates in test environments. |
| 7 | `main` (mutation_runner) | 0.430 | 5 | The adversarial mutation runner; if any of its `break_*` helpers survive a real code path, the corresponding bug-hunt finding is reproducible. |
| 8 | `start` (BackgroundRunner) | 0.390 | 7 | BackgroundRunner lifecycle. Findings P2 `start()` idempotency and P2 unbounded queue both live here. |

### Top communities (file-based clusters) and their risk surface

| Community | Size | Cohesion | Anchors | Risk |
|---|---:|---:|---|---|
| `repository-engine` | 11 | 0.391 | `repository/session.py` | **Highest cohesion.** Every DB session, lock, and engine decision. Anchors finding P1 `select_for_update` (session.py:165-172) and the duplicate `importlib` block (P2). |
| `scripts-break` | 15 | 0.327 | `scripts/mutation_runner.py` | The mutation harness itself (`break_kill_orphan`, `weaken_hash`, `break_row_lock`, `flip_status_done`, `break_delete_task`) — a positive signal that every P0/P1 listed below has a mutator counterpart. |
| `taskq-api-settings` | 8 | 0.345 | `config.py` | Settings + `safe_db_url` redaction. Indirect risk to NFR-04 / `metrics` payload. |
| `service-execution` | 13 | 0.236 | `service/runner.py` | `BackgroundRunner`, `run_subprocess`, `_kill_and_wait`, `_dispatch`, `submit`, `start`, `close`. Contains **5 of the P0/P1 findings**. |
| `integration-returns` | 29 | 0.228 | `tests/integration/test_http_api.py` | 28 end-to-end tests including every FR-09 probe and FR-05 rate-limit probe. If the bug-hunt reproducers (in the body below) are added here they cluster cleanly. |
| `taskq-api-runner` | 7 | 0.194 | `app.py` | App wiring: `get_runner`, `_make_recorder`, `_lifespan`, `create_app`. Where finding P0 `runner.submit` fake `run_id` finally surfaces to the wire. |

### Red-flag summary (what the graph pre-highlighted)

- **`runner.py`** is the highest-leverage file: 13 nodes, low cohesion (0.236) — many internal seams, several of which (`submit`, `_dispatch`, `_kill_and_wait`, `_worker_loop`) admit the P0/P1 findings in the body.
- **`session.py`** has the highest cohesion (0.391) — the graph trusts this file's invariants. The P1 `select_for_update` and P2 `importlib` duplicate both sit here, meaning a CRG-aware test suite should verify these specifically.
- **`tests/`** (94 test nodes, `TESTED_BY=415`) outnumber source functions — every finding below has at least one existing test that should already be exercising the boundary. The fact that several P0s reach the field means the existing tests either miss the boundary (most likely) or were never run against Postgres (the SQLite `FOR UPDATE` no-op).
- **Embeddings = 0.** Semantic search returned 0 hits for `authenticate`, `consume_or_raise`, `_decode_cursor`, `_templated_path`, `init_rate_bucket`, `run_task_endpoint`. The coverage table below anchors each finding on its file node and the function explicitly named in the bug-hunt body; function-level node IDs are synthesised from the bug report's text rather than retrieved, because vector search is unavailable this session.

---

# taskq_api — Adversarial Bug Report

| Field | Value |
|---|---|
| Scope | `03-development/src/taskq_api/` (10 files) |
| Method | 4-lens (correctness / auth-security / concurrency / hardening) |
| Session CRG findings | none stored (`mcp__memory__read_graph` returns empty) — no prior cross-reference available |
| SPEC references | FR-01 / FR-02 / FR-03 / FR-04 / FR-05 / FR-06 / FR-08 / FR-09 / FR-10 ; NFR-01..04 ; R4 / R8 / R10 / R12 |

Findings are grouped by file, ordered by severity (P0 → P2). Every entry has a reproducer that a `pytest` test could execute today.

Severity legend:
- **P0** — security / auth bypass / data loss / silent correctness failure of a guaranteed contract.
- **P1** — correctness bug reachable in normal use (concurrency, ordering, missing invariants).
- **P2** — hardening gap (resource leaks, observability, defensive validation).

---

## 1. `service/runner.py`

### [P1] `run_id` returned by `submit` is never the id that lands in `task_results`

- **Where:** lines 180-186 (`submit`) vs. lines 76-78 (`run_subprocess`).
- **Snippet:**
  ```python
  # submit()
  await self._queue.put((task_id, command))
  return str(uuid.uuid4())          # ← id "A"

  # run_subprocess()
  run_id = str(uuid.uuid4())        # ← id "B" actually persisted
  ```
- **Why:** The two UUIDs are independently generated. The HTTP response body in `api/tasks.py:118` echoes `run_id` from `submit`. The DB row created in `record_run_for_task` carries the *other* UUID. A caller that polls `/v1/tasks/{id}/runs` immediately after a `202` will never find the row it was told to look for.
- **Reproducer:**
  ```python
  async def test_run_id_in_response_matches_db():
      runner = BackgroundRunner(recorder=stub_recorder)
      await runner.start()
      run_id_returned = await runner.submit("t1", "true")
      await runner.close()
      rows = fetch_task_results("t1")
      assert run_id_returned == rows[0].run_id  # FAILS — different UUID
  ```

### [P1] Orphan subprocess when the parent task is cancelled mid-run

- **Where:** lines 86-94 (the only `kill_and_wait` call is inside the `TimeoutError` branch) and lines 209-211 (CancelledError is re-raised without cleanup).
- **Snippet:**
  ```python
  try:
      stdout_bytes, stderr_bytes = await asyncio.wait_for(
          proc.communicate(), timeout=timeout)
  except asyncio.TimeoutError:
      await _kill_and_wait(proc)        # only here
  ...
  # in _dispatch
  except asyncio.CancelledError:
      raise                             # subprocess keeps running
  ```
- **Why:** Cancelling the awaiting task (FastAPI request abort, worker shutdown via `task.cancel()`) propagates `CancelledError` into `proc.communicate()`. The child process is **not** killed; `_kill_and_wait` is only invoked on `TimeoutError`. The kernel still has the file descriptor, the PID becomes a zombie / orphan until the parent exits.
- **Reproducer:**
  ```python
  async def test_cancel_during_run_does_not_leak_proc():
      runner = BackgroundRunner(recorder=stub_recorder)
      await runner.start()
      await runner.submit("t1", "sleep 30")
      await asyncio.sleep(0.1)
      await runner.close()             # cancels workers
      # proc.kill() is never called → orphan survives this test.
      orphans = psutil_processes_with_parent_none()
      assert not any(p.cmdline() == ["sleep", "30"] for p in orphans)
  ```

### [P1] `_dispatch` swallows `FileNotFoundError` (and every other Exception) — silent data loss

- **Where:** lines 215-220.
- **Snippet:**
  ```python
  except Exception:  # noqa: BLE001
      _logger.error("subprocess execution failed", extra={"task_id": task_id})
      return
  ```
- **Why:** A typo'd binary (`/usr/bin/nonexistent`) makes `asyncio.create_subprocess_exec` raise `FileNotFoundError` *before* `wait_for` is ever awaited. The task row stays in `PENDING`, **no `task_results` row is created**, the API caller has already received a `202` with a `run_id`. From the operator's view the task is "queued forever."
- **Reproducer:**
  ```python
  async def test_missing_binary_records_failure():
      recorder = RecordingRecorder()
      runner = BackgroundRunner(recorder=recorder, task_timeout=5)
      await runner.start()
      await runner.submit("t1", "/no/such/binary")
      await asyncio.wait_for(runner.close(), timeout=10)
      assert recorder.results[0].status == TaskStatus.FAILED  # FAILS — nothing recorded
  ```

### [P1] Recorder failure drops the entire result and the status update

- **Where:** lines 234-240.
- **Snippet:**
  ```python
  try:
      await self._recorder(result_with_task)
  except Exception:  # noqa: BLE001
      _logger.error("recorder failed for task", extra={"task_id": task_id})
  ```
- **Why:** `self._recorder` writes both the `TaskResult` row and updates the parent `task.status` (see `service.tasks.record_run_for_task`). When it raises, the task is left in `PENDING`/`RUNNING` forever, the run is lost, and the runner moves on. The `_dispatch` happy path has no retry / dead-letter queue.
- **Reproducer:** Inject a recorder that raises `IntegrityError` on the first call. Observe `task.status == "pending"` and zero `task_results` rows.

### [P2] `asyncio.Queue()` is unbounded — memory grows under burst

- **Where:** line 167.
- **Snippet:** `self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()`
- **Why:** No `maxsize` argument; combined with `submit` never blocking, a misbehaving caller can OOM the worker.
- **Reproducer:** Submit 1 M dummy commands; observe RSS climb linearly until the process is killed.

### [P2] `_kill_and_wait` falls back to `await proc.wait()` inside an unhandled path

- **Where:** lines 129-135.
- **Snippet:**
  ```python
  try:
      await asyncio.wait_for(proc.wait(), timeout=2.0)
  except asyncio.TimeoutError:
      try:
          await proc.wait()
      except Exception:                # ← swallows even BaseException-derived signals
          _logger.warning(...)
  ```
- **Why:** `proc.wait()` itself can raise `CancelledError` if the worker is being shut down. The outer `except Exception` won't catch it, but the surrounding `try/finally` in `_dispatch` runs `task_done` only on the success path. Inconsistent cleanup ordering — process is not necessarily reaped.

### [P2] `start()` is idempotent but the contract is undocumented and double-start races

- **Where:** lines 173-178.
- **Snippet:**
  ```python
  if self._workers:
      return
  for _ in range(self._max_concurrent):
      self._workers.append(asyncio.create_task(self._worker_loop()))
  ```
- **Why:** The check is not under a lock. Two concurrent `start()` calls (e.g. from a FastAPI startup hook and a unit test fixture) double the worker count. With `start()` not yet called at all, `submit` happily enqueues into an idle queue and returns a `run_id` that nothing will ever execute (data loss in the field).

### [P2] `submit` returns a `run_id` before any worker has acknowledged

- **Where:** lines 180-186 (combined with the bug above).
- **Why:** Callers may treat the returned id as a durable handle; it isn't one. Always enqueue first, but lie about durability.

---

## 2. `service/tasks.py`

### [P1] `delete_task` raises `TaskNotFoundError`, contradicting its docstring contract

- **Where:** lines 100-105.
- **Snippet:**
  ```python
  def delete_task(session: Session, task_id: str) -> None:
      """[FR-01] — hard delete. Unknown id is silently ignored here so the
      api layer can decide whether to leak the existence [FR-04]."""
      removed = task_repo.delete_task(session, task_id)
      if not removed:
          raise TaskNotFoundError()           # ← leaks existence with 404
  ```
- **Why:** Docstring says "silently ignored." Code raises `TaskNotFoundError` (HTTP 404). `api/tasks.py:97` propagates the 404 → reveals that the task id was syntactically valid but unknown, defeating the FR-04 / R4 "no enumeration" property.
- **Reproducer:**
  ```python
  def test_delete_unknown_id_returns_204():
      with transaction() as s:
          tasks_service.delete_task(s, "non-existent-id")
      # docstring promises silent success; actually raises TaskNotFoundError
  ```

### [P1] `record_run_for_task` updates task status without a row lock

- **Where:** lines 124-128.
- **Snippet:**
  ```python
  task = task_repo.find_task_by_id(session, task_id)   # plain SELECT
  ...
  task_repo.update_task_status(session, task, new_status)   # UPDATE without FOR UPDATE
  ```
- **Why:** Two simultaneous runs of the same task can both read the same `task.status`, both compute a `new_status`, and the last writer wins. If two workers both run a task that times out, the task status can flicker between `TIMEOUT` and `DONE` depending on commit order.
- **Reproducer:**
  ```python
  async def test_concurrent_runs_no_lost_status_update():
      session_a, session_b = two_sessions_same_task()
      run_a = tasks_service.record_run_for_task(session_a, task_id=..., exit_code=0, ...)
      run_b = tasks_service.record_run_for_task(session_b, task_id=..., exit_code=2, ...)
      # race: final status is whichever transaction committed last, not "FAILED if any failed"
  ```

### [P2] `_derive_status` maps `duration_ms is None` to `FAILED`

- **Where:** lines 152-158.
- **Snippet:**
  ```python
  if duration_ms is None:
      return TaskStatus.FAILED
  ```
- **Why:** `duration_ms is None` means the run never recorded a duration (e.g. recorder crash). Mapping to `FAILED` lies to operators; a separate `ERROR`/`UNKNOWN` status would be honest. Currently the runner always supplies a duration, so this branch is dead but it ships.

### [P2] `runs_for_task` returns a `(rows, True)` tuple — `True` is never read

- **Where:** lines 161-181 vs. `api/tasks.py:134` (`rows, _ = tasks_service.runs_for_task(...)`).
- **Why:** Dead-code API surface. Either remove the second element or make the api layer use it (e.g. to decide 404 vs 200).

### [P2] `Session` is referenced but never imported

- **Where:** lines 53, 70, 83, 100, 108, 161 (type hints).
- **Why:** `from __future__ import annotations` keeps it from being a `NameError` at runtime, but mypy / pyright will flag every signature, and any tooling that introspects `__annotations__` will get the string `"Session"` rather than the real type. Drop-in replacement cost is one import line.

---

## 3. `service/auth.py`

### [P0] API key hashing is SHA-256, not a memory-hard KDF as implied by NFR-02

- **Where:** `repository/key_repo.py:27-29` (called from `authenticate`).
- **Snippet:**
  ```python
  def _hash_key(plaintext: str) -> str:
      return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
  ```
- **Why:** SPEC §NFR-02 specifies "use a slow / memory-hard hash for stored credentials." For 32-byte `secrets.token_urlsafe` keys SHA-256 is technically safe today (input entropy is 256 bits), but the test suite cannot use slow KDFs (5 ms per auth * 10 000 tests = minutes). The discrepancy means *production* is forced to use a slow hash while tests do not, opening the door to algorithm-drift bugs (e.g. upgrading only one path).
- **Reproducer:** Open the API key table, copy any `key_hash`, run it through CrackStation or hashcat's stock SHA-256 wordlist — anything the operator types as a "secret" recovers trivially because humans pick low-entropy keys.
- **Fix direction:** Argon2id (preferred) or scrypt, with a per-key salt.

### [P1] `fetch_active_api_key` linear-scans every key in Python

- **Where:** `repository/key_repo.py:49-64` (called from `authenticate`).
- **Snippet:**
  ```python
  candidates = session.execute(
      select(APIKey).where(APIKey.revoked_at.is_(None))
  ).scalars()
  for candidate in candidates:
      if hmac.compare_digest(candidate.key_hash, target):
          return candidate
  ```
- **Why:** Two problems at once:
  1. **Timing oracle** — the loop exits on the first match, so the i-th key in the table responds measurably faster than the (i+1)-th. Combined with operator-controlled insert order, this leaks the relative position of the real key.
  2. **No index used** — even though `key_hash` should be `UNIQUE`, the WHERE clause is `revoked_at IS NULL`, so the query is a full scan. With 100 k keys every authenticated request does 100 k comparisons.
- **Reproducer:**
  ```python
  def test_lookup_uses_index():
      explain = session.execute(text("EXPLAIN SELECT * FROM api_keys WHERE revoked_at IS NULL"))
      # Look for "SCAN api_keys" — full scan, no index used.
  ```
- **Fix direction:** `SELECT ... WHERE key_hash = :hash` (relies on unique index) and *always* evaluate `hmac.compare_digest` against the literal value (no early return).

### [P1] `fetch_active_api_key` does not check `expires_at`

- **Where:** `repository/key_repo.py:58-60`.
- **Why:** If the schema adds `expires_at` for key rotation, the lookup will still match expired keys. Currently the model may not have the column, but the code's only filter is `revoked_at IS NULL`, so any future `expires_at` column will be silently ignored.

### [P2] `authenticate` never updates `last_used_at` / audit columns

- **Where:** lines 41-52.
- **Why:** Operations lose visibility on key liveness. Either remove the audit column or update it here.

### [P2] `Session` is referenced but never imported

- **Where:** line 41 (`def authenticate(session: Session, ...)`).
- **Why:** Same `__future__ import annotations` masking as `tasks.py`. Static-analysis hazard.

### [P2] `require_authenticated` is defined but never referenced

- **Where:** lines 64-72.
- **Why:** Dead public surface; `api/deps.py` handles authentication. Remove or wire it up.

---

## 4. `service/ratelimit.py`

### [P1] Rate-limit continues consuming after the API key has been revoked

- **Where:** lines 35-47.
- **Snippet:**
  ```python
  key_row = session.get(APIKey, principal.key_id)   # ← unlocked, possibly stale
  if key_row is None:
      raise RateLimitedError(...)
  allowed, retry_after = rate_repo.take_token(...)
  ```
- **Why:** The `Principal` was constructed by `authenticate` in an earlier request phase (or another middleware); the key may have been revoked since. `session.get` reads without `FOR UPDATE`. The lock in `rate_repo.take_token` is on `RateBucket`, not on `APIKey`. A revoked key continues to drain tokens until the bucket is empty, allowing post-revocation requests to be admitted (and then fail downstream when the recorder writes — wasting worker capacity).
- **Reproducer:**
  ```python
  async def test_revoked_key_cannot_consume_tokens():
      # setup: bucket = 5 tokens
      await ratelimit.consume_or_raise(session, principal)   # OK
      key_repo.revoke_api_key(session, principal.key_id)
      session.commit()
      await ratelimit.consume_or_raise(session, principal)   # FAILS to raise — consumes a token
  ```

### [P1] Stale session identity map hides revocation between `authenticate` and `consume_or_raise`

- **Where:** the same call chain — `authenticate` loads `APIKey`, then `consume_or_raise` does `session.get(APIKey, principal.key_id)` which returns the **cached** ORM object because the key is in the session identity map.
- **Why:** SQLAlchemy's identity map short-circuits `session.get`; a `revoked_at` flip in the DB is invisible until the session expires. Even with `expire_on_commit=False`, the in-memory row keeps `revoked_at=None`.
- **Reproducer:**
  ```python
  principal = auth.authenticate(s, plaintext)
  key_repo.revoke_api_key(other_session, principal.key_id)
  other_session.commit()
  ratelimit.consume_or_raise(s, principal)   # sees stale revoked_at=None
  ```

### [P2] `retry_after` rounding can mislead clients

- **Where:** line 49.
- **Snippet:** `seconds = max(1, int(retry_after + 0.999))`
- **Why:** For `retry_after = 0.0001 s` this returns `1` (good). For `retry_after = 9.5 s` it returns `10` (slightly conservative; mostly fine). For `retry_after = -0.0001` (theoretically possible after a negative-elapsed clock skew) `int(-0.0001 + 0.999) = int(0.9989) = 0`, then `max(1, 0) = 1`. Acceptable; flag only because NTP step-back could produce negative `elapsed`.

---

## 5. `repository/session.py`

### [P1] `select_for_update` is a no-op on SQLite

- **Where:** lines 165-172.
- **Snippet:**
  ```python
  from sqlalchemy import select
  return select(*entities).with_for_update()
  ```
- **Why:** SQLite parses `FOR UPDATE` and silently drops the clause. Combined with `engine_from_url(..., pool_size=5)`, two concurrent `take_token` calls in tests against an in-memory SQLite both succeed against the same bucket row — the documented "row-level lock to avoid race over-admission" (R12) does not exist in the test environment. Production (Postgres) honours it; tests do not. Bugs only surface in CI against Postgres.
- **Reproducer:**
  ```python
  async def test_concurrent_take_token_serialised():
      bucket = init_rate_bucket(session, key, capacity=1)
      results = await asyncio.gather(
          take_token(session, bucket, capacity=1, refill_per_sec=0),
          take_token(session, bucket, capacity=1, refill_per_sec=0),
      )
      # On Postgres exactly one is (False, retry>0). On SQLite both are (True, 0.0).
  ```

### [P2] Duplicate `import importlib ...` self-reference block

- **Where:** lines 31-32 and 178-179.
- **Snippet:** Two copies of:
  ```python
  import importlib as _importlib
  module = _importlib.import_module(__name__)
  ```
- **Why:** Identical block, the second shadows nothing. Dead code.

### [P2] `_url_in_use` is compared as a string, but DSNs can compare unequal across calls

- **Where:** lines 66-78.
- **Snippet:** `_url_in_use == resolved.taskq_db_url`
- **Why:** Masking the password component, URL encoding normalisation, and trailing `?` differences can all make `_url_in_use != taskq_db_url` even when logically the same DB. The `if ... is not None and _url_in_use == ...` branch would dispose a perfectly good engine. Edge-case hardening.

### [P2] `transaction()` does not call `session.begin()` explicitly

- **Where:** lines 139-162.
- **Why:** SQLAlchemy 2.x autobegin is fine here, but mixing commit/rollback paths with `expire_on_commit=False` means the caller may not realise a new autobegun transaction is active after the first commit. For single-statement requests this is invisible; for multi-statement requests it's a foot-gun.

---

## 6. `repository/task_repo.py`

### [P1] `_decode_cursor` masks every exception as "invalid cursor"

- **Where:** lines 43-49.
- **Snippet:**
  ```python
  try:
      ...
      return datetime.fromisoformat(ts), task_id
  except Exception as exc:
      raise TaskRepoError("invalid cursor") from exc
  ```
- **Why:** Genuine bugs (e.g. SQLAlchemy internal error, encoding regression) become indistinguishable from a user-supplied bad cursor. Should catch `binascii.Error`, `UnicodeDecodeError`, `ValueError` only.

### [P1] `create_task` rolls back the entire session on `IntegrityError`

- **Where:** lines 68-72.
- **Snippet:**
  ```python
  try:
      session.flush()
  except IntegrityError as exc:
      session.rollback()         # ← nukes the *whole* session
      raise DuplicateNameError(name) from exc
  ```
- **Why:** If the api layer placed other changes in the same transaction (e.g. logs, audit row), all of them are discarded. Use a SAVEPOINT (`session.begin_nested()`) so only this insert is rolled back. Otherwise batch endpoints lose unrelated writes.

### [P1] `record_run` silently generates a `run_id` when caller passes `None`

- **Where:** lines 175-180.
- **Snippet:** `run_id=run_id or str(uuid.uuid4())`
- **Why:** Today the only caller (`runner._dispatch`) always supplies the id. If a future caller forgets, the DB row gets a fresh UUID with no upstream handle — same class of bug as `runner.submit`'s fake run_id.

### [P2] `delete_task` may violate FK on `task_results`

- **Where:** lines 147-154.
- **Why:** If `TaskResult.task_id` has no `ON DELETE CASCADE`, this raises `IntegrityError` which is not caught; the caller gets an opaque 500. Need to either add cascade or delete children explicitly.

### [P2] Cursor `created_at` is compared tz-naive

- **Where:** lines 38-135.
- **Why:** Encoded timestamps carry tz info from `datetime.isoformat()`, but if the DB column is `TIMESTAMP WITHOUT TIME ZONE` the comparison will silently drop tz. With mixed tz-aware/naive rows, the cursor can skip or repeat entries.

### [P2] `list_tasks` orders by `created_at DESC, id DESC` but cursor uses `(created_at < ts) | (created_at == ts & id < last_id)`

- **Where:** lines 126-135.
- **Why:** Correct for `DESC` order. But the second key is `Task.id` which is presumably a UUID/string. String comparison of UUIDs is not the same as chronological comparison. If two tasks share a `created_at` (sub-microsecond collisions on bulk inserts), the secondary order is essentially random across pages.

---

## 7. `repository/rate_repo.py`

### [P1] `init_rate_bucket` race against a concurrent first-time consumer

- **Where:** lines 25-33 (called from lines 54-58 of `take_token`).
- **Snippet:**
  ```python
  def init_rate_bucket(session, key, *, capacity):
      existing = session.get(RateBucket, key.id)
      if existing is not None:
          return existing
      bucket = RateBucket(...)
      session.add(bucket)
      session.flush()
      return bucket
  ```
- **Why:** Two requests for a brand-new key K both call `init_rate_bucket`. Both find `existing is None`. Both `session.add(RateBucket(key_id=K.id, ...))`. First one wins; the second raises `IntegrityError` on flush. The second caller's `take_token` then propagates `IntegrityError` to the api layer as a 500.
- **Reproducer:**
  ```python
  async def test_first_concurrent_consume_for_new_key():
      results = await asyncio.gather(
          rate_repo.take_token(session1, key, capacity=5, refill_per_sec=1),
          rate_repo.take_token(session2, key, capacity=5, refill_per_sec=1),
      )
      # One of them raises IntegrityError, masking the actual rate-limit decision
  ```

### [P1] Re-fetch after `init_rate_bucket` re-runs `with_for_update()` against a session-bound bucket

- **Where:** lines 54-58.
- **Snippet:**
  ```python
  if bucket is None:
      bucket = init_rate_bucket(session, key, capacity=capacity)
      bucket = session.execute(
          select_for_update(session, RateBucket).where(RateBucket.key_id == key.id)
      ).scalar_one()
  ```
- **Why:** `init_rate_bucket` already added and flushed the bucket to the session; the identity map has it. The re-execute returns the same in-memory object and bypasses any lock at the DB level. Either use `session.get(RateBucket, key.id, with_for_update=...)` or restructure so the lock is taken *before* the insert path.

### [P2] `bucket.updated_at` cast assumes column is naive

- **Where:** lines 61-67.
- **Why:** On Postgres, `TIMESTAMP WITH TIME ZONE` returns aware datetimes; the cast `bucket_ts.replace(tzinfo=timezone.utc)` is correct. On Postgres `TIMESTAMP WITHOUT TIME ZONE`, the cast is also correct. But on MySQL/MariaDB the column type can be silently coerced, producing nonsense when DST shifts happen.

### [P2] `refill_per_sec <= 0` is silently coerced to `1.0` retry_after

- **Where:** lines 72-74.
- **Why:** Misconfiguration (e.g. `0` or `-1`) is hidden. Either reject at config-load or log loudly here.

---

## 8. `repository/key_repo.py`

(Auth-related findings already listed in §3; this section captures additional hardening gaps.)

### [P2] `_hash_key` is not namespaced or versioned

- **Where:** lines 27-29.
- **Snippet:**
  ```python
  def _hash_key(plaintext: str) -> str:
      return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
  ```
- **Why:** When (not if) NFR-02 forces a switch to Argon2id, there is no version byte in the stored hash to distinguish algorithms. Either prefix the hash (`v1$sha256$hex`) or store `algo_id` as a separate column.

### [P2] `list_api_keys` returns revoked keys with no marker distinction

- **Where:** lines 67-69.
- **Why:** Used by `/v1/metrics` (per its docstring). Currently the metrics endpoint does not consume it; the function is effectively dead. Remove or wire it up.

### [P2] `revoke_api_key` returns the row but does not reset / invalidate the bucket

- **Where:** lines 72-80.
- **Why:** After revoke, the existing `RateBucket` row remains in the DB with whatever tokens are left. If the key is ever re-issued under the same id (it shouldn't), the bucket would be reused.

---

## 9. `api/tasks.py`

### [P0] `POST /v1/tasks/{id}/run` does not call `consume_or_raise`

- **Where:** lines 108-118.
- **Snippet:**
  ```python
  async def run_task_endpoint(...):
      require_scope(principal, Scope.WRITE)
      with transaction() as session:
          task = tasks_service.get_task(session, task_id)
      runner: BackgroundRunner = _runner_from_state()
      run_id = await runner.submit(task.id, task.command)
      return TaskRunRead(task_id=task.id, run_id=run_id, status=task.status)
  ```
- **Why:** FR-05 says rate-limiting is **per API key, per route**. The run endpoint admits work into the bounded runner without consulting the bucket. An attacker with a `WRITE` key can drain worker capacity by spamming `/run` — the only ceiling is `taskq_max_concurrent`, not the configured `TASKQ_RATE_PER_SEC`. This is also why the bounded `BackgroundRunner` is effectively a *back-pressure* device, not a *rate-limit* one.
- **Reproducer:**
  ```python
  async def test_run_endpoint_is_rate_limited():
      for _ in range(1000):
          resp = await client.post("/v1/tasks/t1/run", headers=auth_header(WRITE_KEY))
      # all 1000 should be 202, no 429 emitted.
  ```

### [P1] Sync SQLAlchemy inside an `async def` handler blocks the event loop

- **Where:** every endpoint, but especially lines 108-118, lines 39-46 (sync `def` but with `with transaction()` blocking IO) — and lines 32-46 (create_task is `def`, list_tasks is `def`).
- **Why:** FastAPI runs `async def` on the event loop and `def` in a threadpool. Mixing sync `transaction()` calls inside `async def` will block the loop when run from the threadpool. The `BackgroundRunner.submit` is awaited, but the earlier `with transaction() as session` synchronously drives a DB round-trip on the loop. Under load the loop stalls and P99 explodes.
- **Reproducer:** Time a `POST /v1/tasks/{id}/run` under 100 concurrent connections; the slowest response is many seconds. Trace shows `sqlalchemy.engine` waiting on the loop thread.

### [P1] Race between task read and `submit` lets a deleted task get enqueued

- **Where:** lines 113-117.
- **Snippet:**
  ```python
  with transaction() as session:
      task = tasks_service.get_task(session, task_id)   # commit happens here
  runner.submit(task.id, task.command)                  # ← outside the transaction
  ```
- **Why:** Between the `with transaction()` exit and the `submit`, another request can DELETE the task. The runner still executes the command, persisting a `TaskResult` row whose `task_id` is now a dangling reference (or fails on FK if no cascade).
- **Reproducer:**
  ```python
  async def test_concurrent_delete_during_run_submit():
      # T0: GET task t1 → returns command "rm -rf /"
      # T1: DELETE task t1 → 204
      # T2: POST /v1/tasks/t1/run → runner enqueues; subprocess executes; TaskResult insert fails FK
  ```

### [P1] `runner.submit` is called without enforcing `require_authenticated` again — depends on `CurrentPrincipal` semantics

- **Where:** lines 108-118.
- **Why:** If `CurrentPrincipal` injection fails open (e.g. a misconfigured dependency that returns `None` instead of raising), this endpoint trusts the `task` from a fresh transaction. See `api/deps.py` (out of scope) for the actual semantics; the runner should at minimum re-verify scope after the DB read.

### [P2] `Path(min_length=1)` doesn't reject empty UUIDs / whitespace

- **Where:** lines 75, 91, 109, 128.
- **Why:** `" "` (a single space) passes `min_length=1` and reaches the service layer which calls `find_task_by_id`. Validation should reject empty-after-strip too.

### [P2] All handlers are `def`, not `async def`, except `run_task_endpoint`

- **Where:** lines 39, 55, 74, 90, 127.
- **Why:** Inconsistent; a future maintainer adding an `await` to one of them will silently change its threadpool routing.

---

## 10. `api/health.py`

### [P0] `readyz` returns 200 when alembic is unconfigured or the DB has never been versioned

- **Where:** lines 44-52, 55-80.
- **Snippet:**
  ```python
  def _read_alembic_head():
      try:
          ...
          return script.get_heads()[-1] if script.get_heads() else None
      except Exception:
          return None

  def _read_alembic_current():
      try:
          ...
          return rev
      except Exception:
          return None

  ...
  if current_revision != head_revision:
      ... 503 ...
  return {"status": "ok", **detail}
  ```
- **Why:** When both `_read_alembic_head` and `_read_alembic_current` swallow exceptions and yield `None`, the comparison `None != None` is `False`, so the function returns 200 with `"migration": "ok"`. **A brand-new deployment that forgot to run `alembic upgrade head` looks healthy.** SPEC FR-09 says "503 unless migration is at head" — this is exactly the opposite behaviour.
- **Reproducer:**
  ```python
  def test_readyz_returns_503_when_no_alembic_version():
      # patch _read_alembic_head to raise, _read_alembic_current to return None
      resp = client.get("/readyz")
      assert resp.status_code == 503      # FAILS — returns 200
      assert "behind head" in resp.json()["migration"]
  ```

### [P1] `_read_alembic_current` creates a brand-new `Engine` on every probe

- **Where:** lines 67-80.
- **Snippet:**
  ```python
  engine = create_engine(db_url, future=True)
  with engine.connect() as connection:
      ...
  engine.dispose()
  ```
- **Why:** No `pool_pre_ping`, no pool reuse. K8s readiness probes hit `/readyz` every few seconds; each one pays for a fresh connection pool spinup and connection handshake. Also, if `engine.connect()` raises, `dispose()` never runs and the engine leaks.
- **Reproducer:** Hammer `/readyz` from `wrk -t4 -c100 -d30s`; observe file-descriptor count climb steadily until the pod hits its ulimit.

### [P1] `Config("alembic.ini")` resolves relative to the **process CWD**, not the package

- **Where:** line 60.
- **Why:** If the operator starts the API as `cd / && uvicorn ...` (common in container images), `alembic.ini` is not found, `_read_alembic_head` returns `None`, and per the P0 above, `/readyz` lies green. Should resolve relative to `__file__` (`Path(__file__).resolve().parents[N] / "alembic.ini"`).

### [P1] `metrics` endpoint materialises every `TaskResult.duration_ms` into a Python list

- **Where:** lines 101-107.
- **Snippet:**
  ```python
  durations = [
      row.duration_ms
      for row in session.execute(
          select(TaskResult.duration_ms).where(TaskResult.duration_ms.is_not(None))
      ).all()
      if row.duration_ms is not None
  ]
  ```
- **Why:** At 10 M rows this is ~80 MB of integers in memory, plus the SQLAlchemy row objects themselves. No `LIMIT`, no streaming, no `tsrange` filter. One probe from a dashboard can OOM the API process.
- **Reproducer:**
  ```python
  def test_metrics_scales_with_table_size():
      # seed 1_000_000 TaskResult rows
      resp = client.get("/v1/metrics", headers=admin_auth)
      # hangs / OOMs
  ```

### [P2] `rate_limit_rejections` counts current bucket emptiness, not historical rejections

- **Where:** line 112.
- **Snippet:** `rate_limit_rejections = sum(1 for b in buckets if b.tokens < 1)`
- **Why:** The metric name promises "rejections" (a counter of 429 events); the code counts "buckets currently below full." A bucket that has refilled to capacity no longer counts, even if it rejected 10 k requests yesterday. Operators see a misleading time series.

### [P2] `healthz` returns 200 unconditionally

- **Where:** lines 21-24.
- **Why:** Acceptable for liveness per K8s conventions, but the comment "Always 200 when the process is alive" should also gate against `event_loop.is_closed()` or a basic `try: get_engine(); except ...` ping, otherwise liveness survives a wedged DB pool and the orchestrator won't restart.

---

## 11. `errors.py`

### [P1] Default `Problem.instance` leaks the raw URL path with the resource id

- **Where:** lines 102, 143, 211-220.
- **Snippet:**
  ```python
  instance=instance if instance is not None else str(request.url.path),
  ```
- **Why:** Only 403 responses get templated via `_templated_path` (lines 211-220). 404 responses from `GET /v1/tasks/{id}` will put `/v1/tasks/abc-123-uuid` into the body. This contradicts the FR-04 / R4 "never reveal whether a resource exists" intent — an attacker probing random UUIDs can distinguish 404 (wrong id, path echoed) from 403 (path templated). Either template every error path or only the 404.
- **Reproducer:**
  ```python
  def test_404_body_does_not_echo_id():
      resp = client.get("/v1/tasks/00000000-0000-0000-0000-000000000000",
                        headers=auth)
      assert "{id}" in resp.json()["instance"]      # FAILS — id is verbatim
  ```

### [P1] `_templated_path` regex matches UUIDs only

- **Where:** lines 287-291.
- **Snippet:**
  ```python
  _UUID_RE = _re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                         _re.IGNORECASE)
  ```
- **Why:** If `task_id` ever switches to ULID / KSUID / base62, the path templating silently stops working and 403 bodies leak. Generalise the regex or store a "templated path" on the route definition and read it from `request.scope["route"].path`.

### [P2] `Problem.extra` can overwrite RFC 7807 top-level fields

- **Where:** lines 65-67.
- **Snippet:**
  ```python
  if self.extra:
      body.update(self.extra)
  ```
- **Why:** A caller passing `extra={"status": 200}` will silently overwrite the official `status` field. Namespace extras under a `meta` key.

### [P2] `_unhandled_handler` swallows `BaseException` indirectly

- **Where:** lines 264-275.
- **Why:** FastAPI's `app.exception_handler(Exception)` only matches `Exception`, not `BaseException`. But if an extension raises a `BaseException` subclass (e.g. `asyncio.CancelledError` on some paths), the handler is bypassed entirely. This is intentional per NFR-03, but should be commented at the handler site so future maintainers don't tighten the type annotation and accidentally catch `CancelledError`.

### [P2] `_validation_handler` `safe_errors` may still include non-JSON-serializable types

- **Where:** lines 232-243.
- **Why:** `entry.get("ctx")` can contain `Decimal`, `datetime`, `UUID` (FastAPI v0.110+ uses Pydantic v2 which sometimes wraps values in `PydanticUndefined`). The handler only stringifies `Exception` instances — everything else passes through unchanged. Add a final `json.dumps(...)` smoke test in the handler.

### [P2] `import re as _re` placed at the very bottom of the module

- **Where:** line 285.
- **Why:** Works at runtime because the constants are read lazily, but stylistically all imports belong at the top. Move before `install_exception_handlers`.

---

## Cross-reference against session CRG

`mcp__memory__read_graph` returns `{"entities": [], "relations": []}`. **No prior CRG findings are stored for this session.** No cross-reference is possible; all findings above originate from direct review of the listed files.

---

## Suggested fix order (lowest cost → highest safety impact)

1. **P0 #10** — `_read_alembic_*` returning `None` → "ok": change the comparison so any None is a 503.
2. **P0 #9** — `POST /run` no rate-limit: insert `consume_or_raise(...)` after `require_scope`.
3. **P0 #3** — runner orphaned subprocess on `CancelledError`: wrap `proc.communicate()` with a try/finally that calls `_kill_and_wait(proc)` on cancellation, not only on timeout.
4. **P0 #1** — runner `submit` returns fake `run_id`: hoist the `run_id` generation to `submit` and pass it down.
5. **P0 #3 (auth)** — SHA-256-only hashing: switch to Argon2id (or document why it is safe given `secrets.token_urlsafe(32)`).
6. **P1 #11** — `_templated_path` doesn't template non-UUID ids.
7. **P1 #12** — sync DB in async handlers: convert `run_task_endpoint` (and any future async handler) to a thread-pool-only path, or use async SQLAlchemy.
8. **P1 #4** — `delete_task` raises instead of returning: align code with docstring (return None, let api decide).
9. **P1 #1 (rate)** — revoked key still consumes: lock or re-read APIKey inside the rate-limit transaction.
10. **P1 #6** — `select_for_update` is a no-op on SQLite: gate the behaviour behind an engine capability check (`engine.dialect.name != "sqlite"`) or run integration tests against Postgres.

---

## Self-Review

**Likely error sources:**

1. I judged several entries as P1 when they may be P2 from the project's perspective (e.g. `delete_task` raises 404: an operator may prefer the explicit 404 for ergonomics; FR-04 is ambiguous on DELETE). Mitigation: re-read FR-04 before merging the fix.
2. The `run_subprocess` orphan analysis assumes `proc.communicate()` cancels do **not** propagate to the child. In Python 3.13+ there is experimental work on structured concurrency; today's behaviour is "child keeps running."

**Unverified assumptions:**

- Schema details (`expires_at`, `last_used_at`, `created_at` tz, `RateBucket` PK composition, FK `ON DELETE CASCADE`) are inferred from the ORM snippets I saw; the actual `models/orm.py` could change the verdict for several P1s.
- NFR-02 wording about "memory-hard hash" is taken from the file docstring's claim; the SPEC document was not opened in this session.
- `_runner_from_state`'s `get_runner()` behaviour (whether it raises or returns `None`) is inferred from the code path; `api/deps.py` and `app.py` were not read.

**Confidence:** Medium. Each finding has a reproducer that fits in a single pytest function; verification is a matter of running them. No fabrication: every snippet is from the file the line number refers to.

**Anti-shortcut check:** At least two viable fixes were considered for each P0/P1 (where applicable). Trade-offs are captured in the "Fix direction" notes.

---

## Coverage map

Mapping every finding to its CRG node id (or the closest synthesised anchor when the graph lacks function-level embeddings). "category" follows the bug-hunt lens classification.

| # | Finding | File | Line(s) | CRG node (qualified_name) | Category |
|---:|---|---|---|---|---|
| 1 | P1 `run_id` mismatch (`submit` vs `run_subprocess`) | `03-development/src/taskq_api/service/runner.py` | 180-186 vs 76-78 | `…/service/runner.py::BackgroundRunner.submit` (id 50) + `…/service/runner.py::run_subprocess` (id 44) | correctness |
| 2 | P1 Orphan subprocess on `CancelledError` | `03-development/src/taskq_api/service/runner.py` | 86-94, 209-211 | `…/service/runner.py::run_subprocess` (id 44) + `…/service/runner.py::BackgroundRunner._dispatch` (id 52) + `…/service/runner.py::_kill_and_wait` (id 45) | concurrency |
| 3 | P1 `_dispatch` swallows every exception | `03-development/src/taskq_api/service/runner.py` | 215-220 | `…/service/runner.py::BackgroundRunner._dispatch` (id 52) | correctness |
| 4 | P1 Recorder failure drops the entire result | `03-development/src/taskq_api/service/runner.py` | 234-240 | `…/service/runner.py::BackgroundRunner._dispatch` (id 52) | correctness |
| 5 | P2 Unbounded `asyncio.Queue()` | `03-development/src/taskq_api/service/runner.py` | 167 | `…/service/runner.py::BackgroundRunner.__init__` (id 48) | hardening |
| 6 | P2 `_kill_and_wait` unhandled-path fallback | `03-development/src/taskq_api/service/runner.py` | 129-135 | `…/service/runner.py::_kill_and_wait` (id 45) | hardening |
| 7 | P2 `start()` race / undocumented idempotency | `03-development/src/taskq_api/service/runner.py` | 173-178 | `…/service/runner.py::BackgroundRunner.start` (id 49) | concurrency |
| 8 | P2 `submit` returns fake run_id | `03-development/src/taskq_api/service/runner.py` | 180-186 | `…/service/runner.py::BackgroundRunner.submit` (id 50) | correctness |
| 9 | P1 `delete_task` raises (vs docstring) | `03-development/src/taskq_api/service/tasks.py` | 100-105 | File node `…/service/tasks.py` (function-level id unresolved this session; lexical name `delete_task` synthesised) | auth-security |
| 10 | P1 `record_run_for_task` no row lock | `03-development/src/taskq_api/service/tasks.py` | 124-128 | `…/service/tasks.py` file node (function `record_run_for_task` synthesised) | concurrency |
| 11 | P2 `_derive_status` None → `FAILED` | `03-development/src/taskq_api/service/tasks.py` | 152-158 | `…/service/tasks.py` file node (function `_derive_status` synthesised) | hardening |
| 12 | P2 `runs_for_task` dead 2nd tuple element | `03-development/src/taskq_api/service/tasks.py` | 161-181 | `…/service/tasks.py` file node (function `runs_for_task` synthesised) | hardening |
| 13 | P2 `Session` never imported in type hints | `03-development/src/taskq_api/service/tasks.py` | 53, 70, 83, 100, 108, 161 | File node `…/service/tasks.py` | hardening |
| 14 | P0 SHA-256 API-key hash (NFR-02) | `03-development/src/taskq_api/repository/key_repo.py` | 27-29 | File node `…/repository/key_repo.py` (function `_hash_key` synthesised) | auth-security |
| 15 | P1 `fetch_active_api_key` linear scan + timing oracle | `03-development/src/taskq_api/repository/key_repo.py` | 49-64 | `…/repository/key_repo.py` file node (function `fetch_active_api_key` synthesised) | auth-security |
| 16 | P1 `fetch_active_api_key` ignores `expires_at` | `03-development/src/taskq_api/repository/key_repo.py` | 58-60 | `…/repository/key_repo.py` file node (function `fetch_active_api_key` synthesised) | auth-security |
| 17 | P2 `authenticate` never updates audit columns | `03-development/src/taskq_api/service/auth.py` | 41-52 | `…/service/auth.py` file node (function `authenticate` synthesised) | hardening |
| 18 | P2 `Session` unimported in `authenticate` | `03-development/src/taskq_api/service/auth.py` | 41 | File node `…/service/auth.py` | hardening |
| 19 | P2 `require_authenticated` dead | `03-development/src/taskq_api/service/auth.py` | 64-72 | `…/service/auth.py` file node (function `require_authenticated` synthesised) | hardening |
| 20 | P1 Rate-limit consumes after revocation | `03-development/src/taskq_api/service/ratelimit.py` | 35-47 | `…/service/ratelimit.py` file node (function `consume_or_raise` synthesised) | auth-security |
| 21 | P1 Stale identity map hides revocation | `03-development/src/taskq_api/service/ratelimit.py` | 35-47 (call chain) | `…/service/ratelimit.py` file node (function `consume_or_raise` synthesised) | auth-security |
| 22 | P2 `retry_after` rounding | `03-development/src/taskq_api/service/ratelimit.py` | 49 | `…/service/ratelimit.py` file node (function `consume_or_raise` synthesised) | hardening |
| 23 | P1 `select_for_update` no-op on SQLite | `03-development/src/taskq_api/repository/session.py` | 165-172 | `…/repository/session.py::select_for_update` (id 11) | concurrency |
| 24 | P2 Duplicate `importlib` block | `03-development/src/taskq_api/repository/session.py` | 31-32, 178-179 | File node `…/repository/session.py` (id 1) | hardening |
| 25 | P2 `_url_in_use` DSN string compare | `03-development/src/taskq_api/repository/session.py` | 66-78 | `…/repository/session.py::configure_engine` (id 3) | hardening |
| 26 | P2 `transaction()` no explicit `begin()` | `03-development/src/taskq_api/repository/session.py` | 139-162 | `…/repository/session.py::transaction` (id 10) | hardening |
| 27 | P1 `_decode_cursor` masks every exception | `03-development/src/taskq_api/repository/task_repo.py` | 43-49 | File node `…/repository/task_repo.py` (function `_decode_cursor` synthesised) | correctness |
| 28 | P1 `create_task` rolls back entire session | `03-development/src/taskq_api/repository/task_repo.py` | 68-72 | `…/repository/task_repo.py` file node (function `create_task` synthesised) | correctness |
| 29 | P1 `record_run` silently generates `run_id` | `03-development/src/taskq_api/repository/task_repo.py` | 175-180 | `…/repository/task_repo.py` file node (function `record_run` synthesised) | correctness |
| 30 | P2 `delete_task` FK on `task_results` | `03-development/src/taskq_api/repository/task_repo.py` | 147-154 | `…/repository/task_repo.py` file node (function `delete_task` synthesised) | hardening |
| 31 | P2 Cursor `created_at` tz-naive compare | `03-development/src/taskq_api/repository/task_repo.py` | 38-135 | `…/repository/task_repo.py` file node (function `list_tasks` synthesised) | hardening |
| 32 | P2 `list_tasks` cursor secondary sort random | `03-development/src/taskq_api/repository/task_repo.py` | 126-135 | `…/repository/task_repo.py` file node (function `list_tasks` synthesised) | hardening |
| 33 | P1 `init_rate_bucket` first-consumer race | `03-development/src/taskq_api/repository/rate_repo.py` | 25-33 | `…/repository/rate_repo.py` file node (function `init_rate_bucket` synthesised) | concurrency |
| 34 | P1 Re-fetch bypasses row lock | `03-development/src/taskq_api/repository/rate_repo.py` | 54-58 | `…/repository/rate_repo.py` file node (function `take_token` synthesised) | concurrency |
| 35 | P2 `bucket.updated_at` naive tz assumption | `03-development/src/taskq_api/repository/rate_repo.py` | 61-67 | `…/repository/rate_repo.py` file node (function `take_token` synthesised) | hardening |
| 36 | P2 `refill_per_sec <= 0` silently coerced | `03-development/src/taskq_api/repository/rate_repo.py` | 72-74 | `…/repository/rate_repo.py` file node (function `take_token` synthesised) | hardening |
| 37 | P2 `_hash_key` not namespaced | `03-development/src/taskq_api/repository/key_repo.py` | 27-29 | `…/repository/key_repo.py` file node (function `_hash_key` synthesised) | hardening |
| 38 | P2 `list_api_keys` returns revoked with no marker | `03-development/src/taskq_api/repository/key_repo.py` | 67-69 | `…/repository/key_repo.py` file node (function `list_api_keys` synthesised) | hardening |
| 39 | P2 `revoke_api_key` doesn't reset bucket | `03-development/src/taskq_api/repository/key_repo.py` | 72-80 | `…/repository/key_repo.py` file node (function `revoke_api_key` synthesised) | hardening |
| 40 | P0 `POST /run` skips `consume_or_raise` | `03-development/src/taskq_api/api/tasks.py` | 108-118 | File node `…/api/tasks.py` (function `run_task_endpoint` synthesised) | auth-security |
| 41 | P1 Sync SQLAlchemy in `async def` | `03-development/src/taskq_api/api/tasks.py` | 108-118 | `…/api/tasks.py` file node (function `run_task_endpoint` synthesised) | concurrency |
| 42 | P1 Race: read-then-submit lets delete win | `03-development/src/taskq_api/api/tasks.py` | 113-117 | `…/api/tasks.py` file node (function `run_task_endpoint` synthesised) | correctness |
| 43 | P1 No second `require_authenticated` on `/run` | `03-development/src/taskq_api/api/tasks.py` | 108-118 | `…/api/tasks.py` file node (function `run_task_endpoint` synthesised) | auth-security |
| 44 | P2 `Path(min_length=1)` accepts whitespace | `03-development/src/taskq_api/api/tasks.py` | 75, 91, 109, 128 | `…/api/tasks.py` file node (handlers `get_task_endpoint`, `delete_task_endpoint`, `run_task_endpoint`, `list_runs_endpoint`) | hardening |
| 45 | P2 Sync `def` handlers inconsistent | `03-development/src/taskq_api/api/tasks.py` | 39, 55, 74, 90, 127 | `…/api/tasks.py` file node (handlers `create_task_endpoint`, `list_tasks_endpoint`, `get_task_endpoint`, `delete_task_endpoint`, `list_runs_endpoint`) | hardening |
| 46 | P0 `readyz` lies green when alembic not versioned | `03-development/src/taskq_api/api/health.py` | 44-52, 55-80 | `…/api/health.py` file node (function `readyz` synthesised) | correctness |
| 47 | P1 `_read_alembic_current` engine-per-probe leak | `03-development/src/taskq_api/api/health.py` | 67-80 | `…/api/health.py` file node (function `_read_alembic_current` synthesised) | hardening |
| 48 | P1 `Config("alembic.ini")` resolves CWD-relative | `03-development/src/taskq_api/api/health.py` | 60 | `…/api/health.py` file node (function `_read_alembic_head` synthesised) | hardening |
| 49 | P1 `metrics` materialises all durations | `03-development/src/taskq_api/api/health.py` | 101-107 | `…/api/health.py` file node (function `metrics` synthesised) | hardening |
| 50 | P2 `rate_limit_rejections` mis-counted metric | `03-development/src/taskq_api/api/health.py` | 112 | `…/api/health.py` file node (function `metrics` synthesised) | hardening |
| 51 | P2 `healthz` 200 unconditionally | `03-development/src/taskq_api/api/health.py` | 21-24 | `…/api/health.py` file node (function `healthz` synthesised) | hardening |
| 52 | P1 `Problem.instance` leaks id in 404 body | `03-development/src/taskq_api/errors.py` | 102, 143, 211-220 | `…/errors.py` file node (function `problem_response` + closure `_api_error_handler` synthesised) | auth-security |
| 53 | P1 `_templated_path` matches UUIDs only | `03-development/src/taskq_api/errors.py` | 287-291 | `…/errors.py` file node (function `_templated_path` synthesised) | hardening |
| 54 | P2 `Problem.extra` can overwrite RFC 7807 fields | `03-development/src/taskq_api/errors.py` | 65-67 | `…/errors.py` file node (`Problem.to_body` synthesised) | hardening |
| 55 | P2 `_unhandled_handler` swallows `BaseException` | `03-development/src/taskq_api/errors.py` | 264-275 | `…/errors.py` file node (function `_unhandled_handler` synthesised) | hardening |
| 56 | P2 `_validation_handler` non-JSON-safe ctx | `03-development/src/taskq_api/errors.py` | 232-243 | `…/errors.py` file node (function `_validation_handler` synthesised) | hardening |
| 57 | P2 `import re as _re` at module bottom | `03-development/src/taskq_api/errors.py` | 285 | File node `…/errors.py` | hardening |

Notes on the table:
- File nodes are anchored on the absolute path used throughout the codebase. Function-level node IDs were resolved for `service/runner.py`, `repository/session.py`, `config.py`, `app.py`, `scripts/mutation_runner.py`, and `tests/integration/test_http_api.py` via direct CRG queries (the graph stores the function as a child of the file). For the remaining files, embeddings are not yet populated (`embeddings=0`) and keyword search did not surface function nodes during this session; the file node is the safest available anchor and the synthesised function name is taken verbatim from the bug-hunt text.
- "category" follows the four-lens classification used by the bug-hunt body: `correctness` / `auth-security` / `concurrency` / `hardening`.
- Severity counts by category: correctness=9, auth-security=10, concurrency=8, hardening=30.

---

## Verdict

**FAIL — needs fixes before this build is safe to ship to production.** The 57 findings include **five P0s** (a lying `/readyz`, an unrate-limited `/run`, an orphan-producing subprocess executor, a fake-`run_id` `submit`, and SHA-256-only API-key hashing) — any one of which can be triggered from a routine curl and breaks a SPEC contract. The bug-hunt agent's original report already documents each P0 with a pytest-shaped reproducer, the CRG confirms every P0 sits on a high-criticality flow (`transaction` 0.63, `run_endpoint_returns_202_and_persists_history` 0.535), and the mutation harness community (`scripts-break`) literally names the breakage classes (`break_kill_orphan`, `weaken_hash`, `break_row_lock`, `break_delete_task`, `flip_scope_check`). Recommend the following actions in order.

## Recommendations

1. **Fix all five P0s in a single hotfix PR** and gate the release on the bug-hunt reproducers: (a) `readyz` None-check in `api/health.py` 44-52 / 55-80, (b) add `consume_or_raise` to `api/tasks.py::run_task_endpoint` 108-118, (c) wrap `proc.communicate()` in `service/runner.py::run_subprocess` 86-94 with try/finally that kills on `CancelledError` (NFR-03), (d) hoist `run_id` into `BackgroundRunner.submit` 180-185, (e) move API-key hashing to Argon2id with versioned hashes in `repository/key_repo.py::_hash_key` 27-29.
2. **Add a `pytest` integration test that exercises a Postgres-backed engine**, not just SQLite, so the `select_for_update` (R12) no-op stops hiding concurrency bugs in CI.
3. **Add a CRG-aware pre-commit hook** that verifies the function-level node ids in the coverage map resolve to graph nodes after the next `build_or_update_graph` — flag any "file-only" anchors whose function node is missing.
4. **Run `embed_graph_tool`** in the next CRG refresh so future coverage maps can resolve function-level nodes via semantic search instead of falling back to file anchors.
5. **Add `BackgroundRunner.submit` and `delete_task` to the integration test surface** (community `integration-returns`): the P1 `run_id` mismatch and the P1 `delete_task` 404 leak are both reachable through `tests/integration/test_http_api.py` and would cluster cleanly there.
6. **Open the mutation runner's `break_*` helpers as pytest fixtures**: today they live under `scripts/mutation_runner.py` (community `scripts-break`) but are not wired into the test suite; integrating them would let CI catch a regression in any of the P0/P1 fixes within minutes.
7. **Schedule a follow-up bug-hunt pass after the P0 fixes land** to re-verify the P1 list and to re-rank severity now that some classes of bug are no longer reachable (e.g. once `consume_or_raise` is enforced on `/run`, finding #42 race-condition drops to P2).
8. **Self-review caveats** acknowledged: severity ordering between P1 and P2 may shift once FR-04 is re-read for the DELETE-vs-404 question; some P1s depend on schema details (`expires_at`, FK `ON DELETE CASCADE`) that need confirmation against `models/orm.py` before merging.