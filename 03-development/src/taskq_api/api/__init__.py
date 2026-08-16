"""L4 — FastAPI routing.

All public handlers stay under 40 lines [NFR-11] — business logic lives in
:mod:`taskq_api.service`. Authn/authz happens through a single dependency
in :mod:`taskq_api.api.deps` [FR-04].
"""

from . import deps, health, tasks  # noqa: F401 — package surface

__all__ = ["deps", "health", "tasks"]
# Convenience re-exports — the app factory grabs these by attribute lookup.
router = None  # placeholder; the real routers live in the submodules.
tasks_router = tasks.router
health_router = health.router