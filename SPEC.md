# taskq-api — 規格文件(單一事實來源)

> 本文件為 `taskq-api` 的完整規格。**所有實作以此文件為準。**
> 專案角色:harness-methodology 漸進式驗證測床**第 2 輪**(Python 後端 + 資料庫)——
> 在第 1 輪點亮的維度基礎上,再補上 **HTTP 層、真實資料庫、真 schema migration、async** 四個前兩個測床完全沒有的面向。
>
> **執行方式**:本檔是規格庫中的第 2 輪規格。開跑時複製到獨立 repo 並更名為
> `SPEC.md`(`PROJECT_BRIEF-2.md` → `PROJECT_BRIEF.md`)—— 框架的
> `canonical_spec` 硬性要求規格位於專案根目錄且檔名為 `SPEC.md`。

---

## 0. 文件元資料

| 欄位 | 值 |
|------|-----|
| 文件版本 | v1.0.0 |
| 專案名稱 | `taskq-api` |
| 驗證輪次 | 第 2 輪 / 共 3 輪 |
| 前一測床 | `taskq-plus`(SPEC.md v1.0.0,8 FR / 12 NFR) |
| 制訂日期 | 2026-07-30 |
| 配套檔案 | `PROJECT_BRIEF-2.md`(10 FR / 12 NFR / 12 env 同步)、`.env.example`、`.importlinter`、`requirements.txt`、`alembic.ini`、`Makefile` |
| 文件責任 | 規格單一真實來源(Single Source of Truth);所有實作以此為準 |
| Phase 1 規範 | Agent A INGESTION MODE — 100% transcribe 全部 `### FR-01..FR-10` 與 `### NFR-01..NFR-12` heading,no invention,no omission |

### 本輪設計意圖(相對第 1 輪新增什麼)

第 1 輪(`taskq-plus`)點亮了 `license_compliance` / `architecture_constraints` /
`mutation_testing` / `test_assertion_quality` 四個維度,但它仍是**單進程 CLI**。
下列面向在前兩個測床都無法產生任何信號:

| 未覆蓋面向 | 本輪對策 | 條款 |
|---|---|---|
| 無 HTTP 層 → `security` 只掃得到 subprocess,authn/authz/輸入邊界零觸發 | REST API + API key 認證 + per-token scope 授權 + rate limit | FR-03/04/05、NFR-02 |
| 無資料庫 → ORM、交易、連線池、N+1 全零 | SQLAlchemy ORM + 明確交易邊界 + N+1 防護斷言 | FR-06、NFR-01 |
| 「schema migration」是自製 JSON `version` 欄位,且測試全 skip | **Alembic 三步真實演進 + 含資料搬遷 + downgrade 可逆** | FR-07、NFR-03 |
| 無 async → Python 的 `async def` 路徑從未被框架的掃描器處理過 | async 端點 + asyncio 背景執行器 | FR-08、NFR-03 |
| 依賴樹淺(2 個直接依賴) | fastapi / sqlalchemy / alembic / uvicorn + 其 transitive deps | NFR-07 |
| 整合測試只有 CLI 子進程 | `httpx.ASGITransport` 端到端,涵蓋認證與錯誤契約 | NFR-10 |

### 變更日誌

| 版本 | 日期 | 動作 | 摘要 |
|------|------|------|------|
| v1.0.0 | 2026-07-30 | initial | 10 FR / 12 NFR / 12 env — 第 2 輪測床基線 |

---

## 1. 概述

- **專案名稱**:`taskq-api`
- **目的**:任務佇列的 HTTP 服務化 — 以 REST API 提交、查詢、執行任務;資料持久化於關聯式資料庫;schema 隨版本演進;支援認證、授權與流量控制
- **語言**:Python 3.11
- **形態**:ASGI 服務,`uvicorn taskq_api.app:app` 啟動;另提供 `python -m taskq_api` 管理入口(migrate / seed / healthcheck)

---

## 2. 技術架構

| 元件 | 技術 |
|------|------|
| HTTP 框架 | FastAPI(ASGI) |
| 資料驗證 | `pydantic` v2 request/response 模型 |
| ORM | SQLAlchemy 2.x(declarative + `Session` 明確交易邊界) |
| 資料庫 | SQLite(開發/測試)、PostgreSQL(生產)——同一份 ORM 模型 |
| Migration | **Alembic**(v1 → v2 → v3,每步都有 `downgrade`) |
| 非同步 | `async def` 端點 + `asyncio.TaskGroup` 背景執行器 |
| 認證 | `X-API-Key` header,金鑰雜湊後比對(不存明文) |
| 授權 | per-token scope:`read` / `write` / `admin` |
| 流量控制 | per-token 令牌桶(token bucket) |
| 錯誤契約 | RFC 7807 `application/problem+json` |
| 任務執行 | `asyncio.create_subprocess_exec`(禁 `shell=True`) |
| 分層約束 | `import-linter` layers contract(見 NFR-06) |

---

## 3. 功能需求(Functional Requirements)

### FR-01: 任務資源 CRUD API

| 方法 | 路徑 | scope | 行為 |
|------|------|-------|------|
| `POST` | `/v1/tasks` | `write` | 建立任務;body 由 `TaskCreate` pydantic 模型驗證 |
| `GET` | `/v1/tasks/{id}` | `read` | 取得單一任務全欄位 |
| `GET` | `/v1/tasks` | `read` | 分頁列表,支援 `?status=`、`?limit=`、`?cursor=` |
| `DELETE` | `/v1/tasks/{id}` | `admin` | 刪除任務(連同結果列,同一交易) |

- 驗證規則同第 1 輪 FR-01(非空 / ≤1000 字元 / 注入字元黑名單 / 名稱唯一);違反 → **HTTP 422** + problem+json
- 未知 id → **HTTP 404** + problem+json
- 分頁為 **cursor-based**(不得用 offset —— 大表 offset 掃描是 N+1 的親戚)
- 列表端點的預設 `limit` 為 50,上限 200;超過上限 → 422

### FR-02: 任務執行端點

- `POST /v1/tasks/{id}/run`(scope `write`)→ **HTTP 202 Accepted**,body 含 `run_id`
- 實際執行以 `asyncio.create_subprocess_exec(*shlex.split(command))` 進行,**禁 `shell=True`**,timeout 為 `TASKQ_TASK_TIMEOUT`
- 狀態機:`pending → running → done | failed | timeout`
- 執行結果寫入 `task_results` 表(FR-07 的 v3 schema),欄位:`exit_code` / `stdout_tail` / `stderr_tail` / `duration_ms` / `finished_at`
- `GET /v1/tasks/{id}/runs`(scope `read`)→ 該任務的歷史執行紀錄,新到舊排序

### FR-03: API Key 認證

- 全部 `/v1/*` 端點要求 `X-API-Key` header;缺少或無效 → **HTTP 401** + problem+json
- 金鑰**以 SHA-256 雜湊儲存**於 `api_keys` 表,**不得存明文**;比對用 `hmac.compare_digest`(常數時間)
- 金鑰由 `python -m taskq_api key create --scope <scope>` 產生,明文**只在建立當下印出一次**
- 停用金鑰:`revoked_at` 非空的金鑰一律視為無效
- `/healthz`、`/readyz` 不要求認證(FR-09)

### FR-04: Scope 授權

- 每把金鑰帶一個 scope:`read` < `write` < `admin`(階層包含)
- 端點所需 scope 見 FR-01/02 表;不足 → **HTTP 403** + problem+json,且 **body 不得洩漏該資源是否存在**
- 授權判定必須在**單一中介層(dependency)**完成,不得散落於各 handler —— 以測試斷言「每個 `/v1` 路由都經過同一個 dependency」

### FR-05: 流量控制

- per-token 令牌桶:容量 `TASKQ_RATE_BURST`,補充速率 `TASKQ_RATE_PER_SEC`
- 超限 → **HTTP 429** + problem+json + `Retry-After` header(秒)
- 令牌桶狀態存於資料庫(跨 worker 一致),更新必須在單一交易內以 row-level lock 進行
- `/healthz`、`/readyz` 不受限

### FR-06: 持久化層與交易邊界

- 全部資料存取經由 `repository/` 層,**業務層不得直接持有 `Session`**
- 每個 API 請求一個 `Session`,交易邊界明確:成功 commit、例外 rollback(以 context manager 保證)
- **禁止字串拼接 SQL**;一律使用 ORM 或參數化查詢(NFR-02)
- 關聯查詢必須用 `selectinload` / `joinedload` 顯式預載 —— **N+1 為驗收失敗條件**(NFR-01)
- 連線池:`pool_size=TASKQ_DB_POOL_SIZE`,`pool_pre_ping=True`

### FR-07: Schema Migration(Alembic 三步演進)

三個 revision,每一步都必須有可運作的 `downgrade`:

| revision | upgrade 內容 | downgrade 要求 |
|---|---|---|
| **v1** | 建立 `tasks`、`api_keys` 兩表 | drop 兩表 |
| **v2** | 新增 `tags`、`task_tags`(多對多)+ `tasks.name` 唯一索引 | drop 新表與索引,不影響 v1 資料 |
| **v3** | **含資料搬遷**:把 `tasks.result_json` 拆為獨立的 `task_results` 表,搬遷既有資料後移除原欄位 | 反向搬遷回 `tasks.result_json` 後 drop `task_results`,**資料不得遺失** |

- `alembic upgrade head` 與 `alembic downgrade base` 必須都成功
- **往返可逆性驗收**:`upgrade head` → 寫入樣本資料 → `downgrade -1` → `upgrade head`,樣本資料的欄位值必須逐欄相同(v3 的資料搬遷是本條的重點)
- 禁止以 `op.execute("DROP TABLE ...")` 之類的破壞性捷徑取代真正的 downgrade
- migration 檔本身納入測試覆蓋(以 `alembic` 的 offline SQL 產生 + 斷言)

### FR-08: 非同步執行器

- 背景執行以 `asyncio.TaskGroup` 管理;服務關閉時必須 **graceful drain**(等待進行中的任務至 `TASKQ_DRAIN_TIMEOUT`,逾時則標記 `interrupted`)
- 併發上限 `TASKQ_MAX_CONCURRENT`;超過時新任務排隊,不得無限制生成 coroutine
- 任務 timeout 以 `asyncio.wait_for` 實作;逾時必須**確實終止子進程**(`process.kill()` 後 `await process.wait()`),不得留下孤兒進程
- 取消語意:`asyncio.CancelledError` 必須向上傳播,**不得被 `except Exception` 吞掉**(NFR-03)

### FR-09: 健康檢查與可觀測性

| 端點 | 認證 | 行為 |
|------|------|------|
| `GET /healthz` | 無 | 進程存活 → 200 `{"status":"ok"}` |
| `GET /readyz` | 無 | DB 連線可用 **且** `alembic current` == head → 200;否則 **503** 並在 body 說明哪一項失敗 |
| `GET /v1/metrics` | `admin` | 任務計數(按狀態)、執行延遲分位數、rate-limit 拒絕數 |

- `/readyz` 的「migration 未到 head」判定是關鍵:部署了新程式碼但忘記跑 migration 時必須 **fail closed**

### FR-10: 錯誤契約(RFC 7807)

- 全部非 2xx 回應的 `Content-Type` 為 `application/problem+json`
- body 欄位:`type`(URI)、`title`、`status`、`detail`、`instance`、`correlation_id`
- **`detail` 不得洩漏內部細節**:不得含 SQL 陳述、堆疊追蹤、檔案路徑、資料庫結構描述
- `correlation_id` 同時出現在回應 header `X-Correlation-Id` 與伺服器日誌,可用於串接
- 錯誤碼對照:422 驗證 / 401 未認證 / 403 scope 不足 / 404 未知資源 / 409 名稱衝突 / 429 超限 / 503 未就緒 / 500 其他

---

## 4. 非功能需求(Non-Functional Requirements)

> **維度映射鐵律**:每一條 NFR 的 `dimension` 欄位都必須是
> `harness/toolchains/registry.py::DIMENSION_TOOLS["python"]` 實際存在的 key。

### NFR-01: 效能與查詢效率

- **dimension**:`performance`
- `GET /v1/tasks/{id}` 在 10,000 筆資料下 **p95 < 30ms**(不含網路,以 ASGI transport 量測)
- `GET /v1/tasks?limit=50` 在 10,000 筆資料下 **p95 < 80ms**
- **N+1 為失敗條件**:列表端點回應一次請求所發出的 SQL 陳述數必須是 **常數**(與回傳筆數無關),以 SQLAlchemy event listener 計數斷言
- 量測方式:`pytest-benchmark`

### NFR-02: HTTP 與資料層安全

- **dimension**:`security`
- 全 codebase 禁用 `shell=True`、`eval(`、`exec(`(grep 0 命中)
- **禁止字串拼接 SQL**:不得出現 f-string / `%` / `+` 組成的 SQL;一律 ORM 或參數化(以 grep + code review 雙重驗證)
- API key **雜湊儲存**,比對用 `hmac.compare_digest`(FR-03)
- 403 回應不得洩漏資源存在性(FR-04)
- 錯誤 body 不得含堆疊/SQL/路徑(FR-10)
- CORS 預設**拒絕所有來源**;允許清單由 `TASKQ_CORS_ORIGINS` 明示
- `bandit -r 03-development/src/`:**0 HIGH、0 MEDIUM**

### NFR-03: 錯誤處理、交易與非同步正確性

- **dimension**:`error_handling`
- 每個請求的交易邊界明確:成功 commit、例外 rollback,以 context manager 保證(FR-06)
- **不得**出現裸 `except:`、`except Exception: pass`
- **`asyncio.CancelledError` 不得被吞掉** —— 必須重新拋出(async 專屬的吞噬陷阱)
- 資料庫連線失敗 → `/readyz` 503 + 明確 detail;不得靜默重試至無限
- 任務 timeout 必須確實終止子進程,不留孤兒(FR-08)
- migration 失敗 → 交易 rollback,資料庫維持在前一個 revision(FR-07)

### NFR-04: 敏感資料遮蔽

- **dimension**:`security`
- `stdout_tail` / `stderr_tail` / 日誌 / 錯誤 body 落盤或送出前,匹配
  `(sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+|postgres(ql)?://[^\s]+)` 的行整行以 `[REDACTED]` 取代
- **資料庫連線字串**(含密碼)不得出現在任何日誌、錯誤訊息或 `/v1/metrics` 回應中
- API key 明文只在 `key create` 當下輸出一次,不得寫入任何持久化位置

### NFR-05: 文件覆蓋

- **dimension**:`documentation`
- 全部公開函式/類別有 docstring 且含 `[FR-XX]` 或 `[NFR-XX]` 引用,覆蓋率 **100%**
- 每個 API 端點在 OpenAPI schema 中有 `summary` 與 `description`(FastAPI 自動產生的 `/openapi.json` 以測試斷言)

### NFR-06: 架構分層契約

- **dimension**:`architecture_constraints`
- 專案根目錄**必須存在 `.importlinter`**,宣告 layers contract:

  ```
  api > service > repository > models
  ```

  上層可 import 下層,**下層不得 import 上層**;`config` 與 `errors` 為 independence 模組
- **額外禁令(forbidden contract)**:`repository` 以外的任何層**不得 import `sqlalchemy`** —— ORM 洩漏到業務層是本輪要防的具體反模式
- `lint-imports` 必須 **exit 0**
- 禁止以刪除 `.importlinter`、萬用字元 `ignore_imports`、或降級 contract 的方式取得通過

### NFR-07: 依賴與授權合規

- **dimension**:`license_compliance`
- 全部 runtime 依賴在 `requirements.txt` 以 `==` 釘版;**transitive 依賴以 lock 檔(`requirements.lock`)完整鎖定**
- 允許的 license:MIT / BSD-2-Clause / BSD-3-Clause / Apache-2.0 / PSF;出現其他 → 該依賴不得使用
- **掃描範圍必須包含完整依賴樹**(直接 + transitive),證據命令:`pip-licenses --format=json --with-system`
- 產出 SBOM 於 `08-config/SBOM.json`,含每個依賴的 `name` / `version` / `license` / `direct|transitive`

### NFR-08: 變異測試

- **dimension**:`mutation_testing`
- `.methodology/harness_config.json` 設 `features.mutation_testing: true`
- **mutation score ≥ 70**
- 範圍限定於 `service/` 與 `repository/` 兩層,並在 `harness_config.json` 註記限定理由(執行時間預算)

### NFR-09: 驗證真實性(零 skip 鐵律)

- **dimension**:`test_assertion_quality`
- **任何 FR / NFR 的驗證測試不得是 `pytest.skip` / `skipif` / `xfail` / 無斷言的 stub**
- `pytest 03-development/tests -q` 的 **skipped 計數必須為 0**
- 每個測試函式至少一個 `assert`(`zero_assert == 0`)
- **反造假條款**:不得以 `--ignore` / `-k` / `--deselect` / `collect_ignore` / 從 `testpaths` 移除目錄的方式排除測試
- **本輪特別條款**:`FR-07` 的三步 migration 必須以**真實資料庫**測試(SQLite 檔案,非 in-memory mock),往返可逆性以實際資料比對驗證。**不得**以「migration 邏輯太難測」為由降級為 skip —— 這正是前兩輪失敗的形態
- `TRACEABILITY_MATRIX.md` 的 `VERIFIED` 只能在測試實際執行並通過時給出

### NFR-10: 整合覆蓋

- **dimension**:`integration_coverage`
- `03-development/tests/integration/` 行覆蓋 **≥ 80%**
- 整合測試以 `httpx.AsyncClient(transport=ASGITransport(app))` 驅動,**不得直接呼叫 handler 函式**
- 至少涵蓋:CRUD 全鏈、401/403/404/409/422/429/503 每個錯誤碼各一例、migration 往返、rate limit 觸發與恢復、graceful drain

### NFR-11: 可讀性

- **dimension**:`readability`
- 專案 MI(LLOC 加權)**≥ 80**;單一函式 CC **≤ 10**
- 單一檔案 ≤ 400 行;單一目錄 ≤ 15 檔
- 每個 API handler ≤ 40 行(業務邏輯必須下沉到 `service/`)

### NFR-12: 系統驗證目標

- **dimension**:`execute_verification_target`
- `Makefile` 的 `verify-system` target 必須串接:
  1. `alembic upgrade head`
  2. 全套測試
  3. 服務啟動 + `/healthz`、`/readyz` 冒煙
  4. `alembic downgrade base` 後再 `upgrade head`(往返驗證)
- `make verify-system` 必須 **exit 0** 並在 stdout 印出 `verify-system: PASS`

---

## 5. 參數配置

### 5.1 環境變數(`config.py` 讀取;`.env.example` 完整宣告)

| 變數 | 預設 | 說明 |
|------|------|------|
| `TASKQ_DB_URL` | `sqlite:///./taskq.db` | 資料庫連線字串(**不得**出現在日誌 — NFR-04) |
| `TASKQ_DB_POOL_SIZE` | `5` | 連線池大小(FR-06) |
| `TASKQ_TASK_TIMEOUT` | `10.0` | 單任務 subprocess timeout(秒) |
| `TASKQ_MAX_CONCURRENT` | `8` | 背景執行併發上限(FR-08) |
| `TASKQ_DRAIN_TIMEOUT` | `30.0` | 關閉時 graceful drain 上限(秒) |
| `TASKQ_RATE_BURST` | `20` | 令牌桶容量(FR-05) |
| `TASKQ_RATE_PER_SEC` | `5.0` | 令牌補充速率(FR-05) |
| `TASKQ_CORS_ORIGINS` | (空字串) | CORS 允許來源,逗號分隔;空 = 全拒(NFR-02) |
| `TASKQ_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `TASKQ_LOG_FORMAT` | `json` | `json` / `text` |
| `TASKQ_HOST` | `127.0.0.1` | 監聽位址(預設**不**對外) |
| `TASKQ_PORT` | `8000` | 監聽埠 |

### 5.2 資料庫 Schema(由 FR-07 的 Alembic revision 定義)

| 表 | revision | 主要欄位 |
|---|---|---|
| `tasks` | v1 | `id`(uuid)、`command`、`name`、`status`、`created_at` |
| `api_keys` | v1 | `id`、`key_hash`(sha256)、`scope`、`created_at`、`revoked_at` |
| `tags` | v2 | `id`、`label` |
| `task_tags` | v2 | `task_id`、`tag_id`(複合主鍵) |
| `task_results` | **v3** | `id`、`task_id`(FK)、`exit_code`、`stdout_tail`、`stderr_tail`、`duration_ms`、`finished_at` |
| `rate_buckets` | v1 | `key_id`(FK)、`tokens`、`updated_at` |

> `tasks.result_json` 在 v1 建立、v3 移除(資料搬遷至 `task_results`)。這一步是 FR-07 往返可逆性驗收的重點。

### 5.3 專案側必備設定檔(非可選)

| 檔案 | 用途 | 對應 |
|------|------|------|
| `.importlinter` | 分層契約 + `sqlalchemy` 禁令 | NFR-06 |
| `requirements.txt` + `requirements.lock` | 釘版 + transitive 鎖定 | NFR-07 |
| `requirements-dev.txt` | `import-linter` / `pip-licenses` / `mutmut` / `pytest-benchmark` / `httpx` | NFR-06/07/08/10 |
| `alembic.ini` + `migrations/versions/` | 三個 revision(FR-07) | FR-07 |
| `.env.example` | 全部 12 個 `TASKQ_*` 逐一宣告並附註解 | §5.1 |
| `.methodology/harness_config.json` | `features.mutation_testing: true`;不得調降 `crg_cohesion_healthy` | NFR-08 |
| `Makefile` | `verify-system`(含 migration 往返) | NFR-12 |

---

## 6. 資料夾結構

```
taskq-api/
├── 03-development/
│   ├── src/taskq_api/
│   │   ├── __init__.py
│   │   ├── __main__.py            # 管理入口(migrate / key create / healthcheck)
│   │   ├── app.py                 # FastAPI app 組裝
│   │   ├── config.py              # TASKQ_* env(independence)
│   │   ├── errors.py              # RFC 7807 problem+json(independence,FR-10)
│   │   ├── models/                # L1 — SQLAlchemy declarative + pydantic schema
│   │   │   ├── __init__.py
│   │   │   ├── orm.py             # 表定義(§5.2)
│   │   │   └── schemas.py         # request/response 模型
│   │   ├── repository/            # L2 — 唯一可 import sqlalchemy 的層(NFR-06)
│   │   │   ├── __init__.py
│   │   │   ├── session.py         # Session + 交易 context manager(FR-06)
│   │   │   ├── task_repo.py
│   │   │   ├── key_repo.py
│   │   │   └── rate_repo.py
│   │   ├── service/               # L3 — 業務邏輯,無 ORM 洩漏
│   │   │   ├── __init__.py
│   │   │   ├── tasks.py           # FR-01
│   │   │   ├── runner.py          # FR-02/08 async 執行器
│   │   │   ├── auth.py            # FR-03/04
│   │   │   └── ratelimit.py       # FR-05
│   │   └── api/                   # L4 最上層 — FastAPI 路由
│   │       ├── __init__.py
│   │       ├── deps.py            # 認證/授權 dependency(FR-04 單一判定點)
│   │       ├── tasks.py           # FR-01/02
│   │       └── health.py          # FR-09
│   └── tests/
│       ├── unit/
│       └── integration/           # httpx ASGITransport(NFR-10)
├── migrations/                    # Alembic(FR-07)
│   ├── env.py
│   └── versions/
│       ├── v1_initial.py
│       ├── v2_tags.py
│       └── v3_split_results.py    # 含資料搬遷 + 可逆 downgrade
├── alembic.ini
├── .importlinter                  # NFR-06
├── .env.example
├── requirements.txt / requirements.lock
├── Makefile                       # NFR-12
├── PROJECT_BRIEF.md
└── SPEC.md                        # 本文件(執行時由 SPEC-2.md 更名而來)
```

**分層規則**:`api > service > repository > models`;`config` / `errors` 為 independence;**`sqlalchemy` 僅 `repository` 可 import**。

---

## 7. 錯誤處理

全部非 2xx 回應為 `application/problem+json`(FR-10)。

| 情況 | HTTP | `type` |
|------|------|--------|
| 請求 body 驗證失敗 | 422 | `/errors/validation` |
| 缺少或無效 API key | 401 | `/errors/unauthenticated` |
| scope 不足 | 403 | `/errors/forbidden`(**不洩漏資源是否存在**) |
| 未知 task id | 404 | `/errors/not-found` |
| 任務名稱衝突 | 409 | `/errors/conflict` |
| 超過 rate limit | 429 | `/errors/rate-limited`(附 `Retry-After`) |
| DB 不可用 / migration 未到 head | 503 | `/errors/not-ready` |
| 任務 timeout | 200(任務狀態 `timeout`) | — |
| 其他未預期例外 | 500 | `/errors/internal`(**detail 不含堆疊/SQL/路徑**) |

`asyncio.CancelledError` **不屬於**上表任何一列 —— 它必須向上傳播,不得轉成 500(NFR-03)。

---

## 8. 驗收標準

> 每條都是可機器判定的單一命令 + 期望輸出。

| # | 命令 | 期望 |
|---|------|------|
| 1 | `pytest 03-development/tests -q` | 全綠,**skipped 計數為 0**(NFR-09) |
| 2 | `pytest 03-development/tests --cov=03-development/src --cov-report=term` | TOTAL **100%** |
| 3 | `pytest 03-development/tests/integration --cov=03-development/src --cov-report=term` | TOTAL **≥ 80%**(NFR-10) |
| 4 | `POST /v1/tasks`(有效 write key) | 201 + task id |
| 5 | `POST /v1/tasks`(無 `X-API-Key`) | **401** + problem+json |
| 6 | `DELETE /v1/tasks/{id}`(write key,非 admin) | **403**,body 不透露該 id 是否存在 |
| 7 | `GET /v1/tasks/{unknown}` | **404** + problem+json |
| 8 | `POST /v1/tasks` 重複 name | **409** |
| 9 | 連續請求超過 `TASKQ_RATE_BURST` | **429** + `Retry-After` header |
| 10 | 停掉 DB 後 `GET /readyz` | **503**,detail 指明 DB 不可用 |
| 11 | `alembic downgrade -1` 後 `GET /readyz` | **503**,detail 指明 migration 未到 head |
| 12 | `alembic upgrade head` → 寫樣本 → `downgrade -1` → `upgrade head` | 樣本資料逐欄相同(**v3 資料搬遷可逆** — FR-07) |
| 13 | `alembic downgrade base` | exit 0,無殘留表 |
| 14 | `GET /v1/tasks?limit=50`(10,000 筆)的 SQL 陳述計數 | **常數**(與筆數無關 — N+1 防護,NFR-01) |
| 15 | `GET /v1/tasks/{id}` p95(10,000 筆) | **< 30ms**(NFR-01) |
| 16 | `grep -rn "shell=True\|eval(\|exec(" 03-development/src/` | **0 命中** |
| 17 | 掃描 SQL 字串拼接(f-string / `%` / `+` 組 SQL) | **0 命中**(NFR-02) |
| 18 | 查 `api_keys` 表 | 無明文金鑰;`key_hash` 為 64 hex(NFR-02) |
| 19 | 觸發 500 後檢查回應 body | 不含堆疊 / SQL / 檔案路徑(FR-10 / NFR-02) |
| 20 | 日誌與 `/v1/metrics` 全文 | 不含 `TASKQ_DB_URL` 的密碼片段(NFR-04) |
| 21 | `lint-imports` | **exit 0**,且 `service`/`api` 層 import `sqlalchemy` 會被擋(NFR-06) |
| 22 | `pip-licenses --format=json --with-system` | 每個依賴 license ∈ allowlist(NFR-07) |
| 23 | `bandit -r 03-development/src/` | 0 HIGH,0 MEDIUM |
| 24 | `mutmut run` 後 `mutmut results` | mutation score **≥ 70**(NFR-08) |
| 25 | 服務關閉時有進行中的任務 | graceful drain;逾時者標記 `interrupted`,無孤兒進程(FR-08) |
| 26 | `grep -c "^TASKQ_" .env.example` | **12**(§5.1 全部宣告) |
| 27 | `make verify-system` | exit 0 且 stdout 含 `verify-system: PASS`(NFR-12) |

---

## 9. 風險矩陣

| ID | 風險 | 影響 | 可能性 | 緩解 |
|----|------|------|--------|------|
| R1 | **v3 資料搬遷遺失資料** | **高** | 中 | 往返可逆性測試以真實 DB 逐欄比對(FR-07 / §8 #12) |
| R2 | SQL injection | 高 | 低 | 禁字串拼接 + ORM/參數化 + grep gate(NFR-02) |
| R3 | API key 洩漏 | 高 | 中 | 雜湊儲存 + 常數時間比對 + 明文只印一次(FR-03) |
| R4 | 403 洩漏資源存在性 | 中 | 中 | 授權判定在資源查詢之前(FR-04 / §8 #6) |
| R5 | N+1 查詢在大表上崩潰 | 高 | 高 | 顯式預載 + SQL 計數斷言(NFR-01 / §8 #14) |
| R6 | 錯誤 body 洩漏內部結構 | 中 | 高 | RFC 7807 固定欄位 + detail 白名單(FR-10) |
| R7 | **`CancelledError` 被吞 → 關閉時卡死** | 中 | 中 | 明文禁令 + 測試斷言(NFR-03) |
| R8 | 任務 timeout 留下孤兒進程 | 中 | 中 | `kill()` + `await wait()`(FR-08 / §8 #25) |
| R9 | 部署後忘記跑 migration | 高 | 中 | `/readyz` fail closed(FR-09 / §8 #11) |
| R10 | 連線池耗盡 | 中 | 中 | `pool_pre_ping` + 併發上限(FR-06/08) |
| R11 | transitive 依賴引入不相容 license | 中 | 中 | lock 檔 + 全樹掃描(NFR-07) |
| R12 | rate bucket 競態導致超放行 | 低 | 中 | 單一交易 + row-level lock(FR-05) |

---

## 10. framework 對齊

| dimension(真實 key) | 工具 | 本規格條款 |
|---|---|---|
| `performance` | pytest-benchmark | NFR-01(含 N+1 防護) |
| `security` | bandit | NFR-02、NFR-04 |
| `error_handling` | ast-error-handling | NFR-03(含 async 取消語意) |
| `documentation` | ast-docstrings | NFR-05 |
| `architecture_constraints` | import-linter | **NFR-06**(layers + `sqlalchemy` forbidden) |
| `license_compliance` | scancode / pip-licenses | **NFR-07**(含 transitive) |
| `mutation_testing` | mutmut | **NFR-08** |
| `test_assertion_quality` | ast-assertions | **NFR-09**(migration 不得 skip) |
| `integration_coverage` | pytest-cov-integration | NFR-10(httpx ASGI) |
| `readability` | readability-v2 | NFR-11 |
| `execute_verification_target` | system-verification | NFR-12(含 migration 往返) |
| `linting` / `type_safety` / `test_coverage` | ruff / pyright / pytest-cov | §8 #1 / #2 + 框架預設門檻 |
| `architecture` | code-review-graph | §6 四層 + migrations |
| `secrets_scanning` | gitleaks | 框架預設門檻 100 |

**CRG 校準鐵律**:`crg_cohesion_healthy` 保持預設值,不得為了讓專案通過而調降。

**高風險模組**:`taskq_api.service.runner`(async 子進程)、`taskq_api.service.auth`(認證授權)、`taskq_api.repository.session`(交易邊界)、`migrations/versions/v3_split_results.py`(資料搬遷)。四者需 per-module TDD 覆蓋。

**async 為本輪新變數**:框架的 `ast-error-handling` / `ast-assertions` 掃描器過去只面對過同步程式碼。若它們在 async 語法上出現誤判或漏判,那本身就是本輪測床要交付的發現 —— 應記入 Phase 4 的 bug hunt,不得靜默繞過。

---

## 11. 監控門檻(Quality Gates 對齊)

| 指標 | 閾值 | 量測方式 |
|------|------|---------|
| `GET /v1/tasks/{id}` p95(10k 筆) | < 30ms | pytest-benchmark(NFR-01) |
| `GET /v1/tasks?limit=50` p95(10k 筆) | < 80ms | pytest-benchmark(NFR-01) |
| 列表端點 SQL 陳述數 | 常數(與筆數無關) | SQLAlchemy event listener(NFR-01) |
| 測試 skip 數 | **0** | `pytest -q` 輸出(NFR-09) |
| 零斷言測試函式數 | **0** | ast-assertions(NFR-09) |
| 行覆蓋率 | 100% | pytest-cov |
| 整合覆蓋率 | ≥ 80% | pytest-cov-integration(NFR-10) |
| migration 往返資料一致 | 100%(逐欄) | 真實 SQLite 檔案測試(FR-07) |
| mutation score | ≥ 70 | mutmut(NFR-08) |
| `lint-imports` 違規 | 0 | import-linter(NFR-06) |
| `service`/`api` 層的 `sqlalchemy` import | 0 | import-linter forbidden contract(NFR-06) |
| 非 allowlist license(含 transitive) | 0 | pip-licenses(NFR-07) |
| SQL 字串拼接命中 | 0 | grep CI gate(NFR-02) |
| bandit HIGH / MEDIUM | 0 / 0 | bandit(NFR-02) |
| 錯誤 body 洩漏內部細節 | 0 | integration test(FR-10) |
| DB 連線字串出現於日誌 | 0 | unit test(NFR-04) |
| 孤兒子進程 | 0 | integration test(FR-08) |
| 專案 MI | ≥ 80 | readability-v2(NFR-11) |
| `make verify-system` | exit 0 | Makefile(NFR-12) |

---

*文件版本:v1.0.0(10 FR / 12 NFR / 12 env)| 2026-07-30 | 漸進式驗證測床第 2 輪*
