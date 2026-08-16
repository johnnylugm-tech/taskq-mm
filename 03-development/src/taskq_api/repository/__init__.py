"""L2 — repository layer.

The only layer that may ``import sqlalchemy`` [NFR-06]. All Session use
goes through :func:`taskq_api.repository.session.transaction`, which
guarantees commit/rollback semantics and a single Session per request
[FR-06 / NFR-03].

The package ``__init__`` is intentionally empty so the upper layers can
import individual submodules without pulling every SQLAlchemy-binding
module into their import graph. Public surface is re-exported here as a
documentation aid but **importing from this module in runtime code is
discouraged** — call sites should reference the submodule directly.
"""

__all__ = [
    "engine_from_url",
    "get_sessionmaker",
    "transaction",
    "create_task",
    "find_task_by_id",
    "list_tasks",
    "delete_task",
    "record_run",
    "runs_for_task",
    "update_task_status",
    "create_api_key",
    "fetch_active_api_key",
    "list_api_keys",
    "revoke_api_key",
    "init_rate_bucket",
    "take_token",
]