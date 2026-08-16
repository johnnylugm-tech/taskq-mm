"""L1 — Pydantic v2 request/response models.

These are the only types that cross the api ↔ service boundary [NFR-06]:
the api layer renders them; the service layer validates input through
them. No SQLAlchemy types appear here.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Generic, List, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .orm import Scope, TaskStatus

# --- validation constants (FR-01) ----------------------------------------
_NAME_MAX = 1000
_INJECTION_CHARS = re.compile(r"[;\n\r\\$`|&]|\x00")
_T = TypeVar("_T")


class TaskCreate(BaseModel):
    """[FR-01] request body for ``POST /v1/tasks``."""

    model_config = ConfigDict(extra="forbid")

    command: str = Field(..., min_length=1, max_length=4096)
    name: str = Field(..., min_length=1, max_length=_NAME_MAX)
    tags: List[str] = Field(default_factory=list)

    @field_validator("command")
    @classmethod
    def _no_injection_chars(cls, value: str) -> str:
        if _INJECTION_CHARS.search(value):
            raise ValueError("command contains disallowed shell metacharacters")
        return value

    @field_validator("name")
    @classmethod
    def _name_no_injection(cls, value: str) -> str:
        if _INJECTION_CHARS.search(value):
            raise ValueError("name contains disallowed shell metacharacters")
        return value

    @field_validator("tags")
    @classmethod
    def _tag_labels_clean(cls, value: List[str]) -> List[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for tag in value:
            stripped = tag.strip()
            if not stripped:
                continue
            if len(stripped) > 64:
                raise ValueError("tag exceeds 64 characters")
            if stripped in seen:
                continue
            seen.add(stripped)
            cleaned.append(stripped)
        return cleaned


class TaskRead(BaseModel):
    """[FR-01] response body for task lookups."""

    id: str
    command: str
    name: str
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
    tags: List[str] = Field(default_factory=list)


class TaskResultRead(BaseModel):
    """[FR-02] response body for ``GET /v1/tasks/{id}/runs``."""

    id: int
    run_id: str
    exit_code: Optional[int]
    stdout_tail: Optional[str]
    stderr_tail: Optional[str]
    duration_ms: Optional[int]
    started_at: datetime
    finished_at: datetime


class TaskRunRead(BaseModel):
    """[FR-02] response body for ``POST /v1/tasks/{id}/run`` (202 Accepted)."""

    task_id: str
    run_id: str
    status: TaskStatus


class CursorPage(BaseModel, Generic[_T]):
    """[FR-01] cursor pagination envelope."""

    items: List[_T]
    next_cursor: Optional[str] = None


class APIKeyCreate(BaseModel):
    """[FR-03] response body for ``key create`` — the plaintext is shown once."""

    id: int
    key: str
    scope: Scope


class MetricsResponse(BaseModel):
    """[FR-09] ``GET /v1/metrics`` envelope."""

    task_counts: dict[str, int]
    run_latency_ms: dict[str, float]
    rate_limit_rejections: int


class ProblemBody(BaseModel):
    """[FR-10] / RFC 7807 — body shape returned on every non-2xx response."""

    type: str
    title: str
    status: int
    detail: str
    instance: str
    correlation_id: str
    errors: Optional[List[Any]] = None