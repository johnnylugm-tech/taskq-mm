"""L1 — declarative SQLAlchemy tables + pydantic request/response schemas.

Models import ``sqlalchemy``; repository uses them for queries; service and
api only ever see the pydantic schemas in :mod:`taskq_api.models.schemas`
[NFR-06].

This package is *not* allowed to import any layer above ``repository``
[NFR-06].
"""

from .orm import (  # noqa: F401 — re-exports for Alembic autogenerate.
    APIKey,
    RateBucket,
    Tag,
    Task,
    TaskResult,
    TaskStatus,
)
from .schemas import (  # noqa: F401 — pydantic surface.
    CursorPage,
    ProblemBody,
    TaskCreate,
    TaskRead,
    TaskResultRead,
    TaskRunRead,
)

__all__ = [
    "APIKey",
    "RateBucket",
    "Tag",
    "Task",
    "TaskResult",
    "TaskStatus",
    "TaskCreate",
    "TaskRead",
    "TaskResultRead",
    "TaskRunRead",
    "CursorPage",
    "ProblemBody",
]