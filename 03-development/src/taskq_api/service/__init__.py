"""L3 — business logic.

The service layer must not import ``sqlalchemy`` [NFR-06]; it talks to the
database exclusively through :mod:`taskq_api.repository`. Each request
is processed in exactly one transaction opened via
:func:`taskq_api.repository.session.transaction`.

Public surface
--------------
- :mod:`taskq_api.service.auth` — API key authentication and scope checks
  [FR-03 / FR-04].
- :mod:`taskq_api.service.ratelimit` — per-token token bucket [FR-05].
- :mod:`taskq_api.service.tasks` — task CRUD business rules [FR-01].
- :mod:`taskq_api.service.runner` — async subprocess executor [FR-02 / FR-08].
"""

from .auth import (  # noqa: F401
    Principal,
    authenticate,
    require_scope,
    scope_satisfies,
)
from .ratelimit import (  # noqa: F401
    consume_or_raise,
)
from .runner import (  # noqa: F401
    BackgroundRunner,
    ExecutionResult,
    run_subprocess,
)
from .tasks import (  # noqa: F401
    TaskNotFoundError,
    create_task,
    delete_task,
    get_task,
    list_tasks,
    record_run_for_task,
    runs_for_task,
)

__all__ = [
    "Principal",
    "authenticate",
    "require_scope",
    "scope_satisfies",
    "consume_or_raise",
    "BackgroundRunner",
    "ExecutionResult",
    "run_subprocess",
    "TaskNotFoundError",
    "create_task",
    "delete_task",
    "get_task",
    "list_tasks",
    "record_run_for_task",
    "runs_for_task",
]