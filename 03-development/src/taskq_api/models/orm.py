"""L1 — SQLAlchemy declarative tables.

This module owns the canonical ORM definitions referenced by Alembic
migrations (FR-07). Schema versions:

- v1: ``tasks`` (with ``result_json``), ``api_keys``, ``rate_buckets``
- v2: ``tags`` and ``task_tags`` (many-to-many) + ``tasks.name`` unique index
- v3: ``task_results`` extracted from ``tasks.result_json`` (with data migration)

The ORM is the single source of truth; migrations are produced from this
file's metadata and validated by the integration suite (FR-07 / NFR-09).
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base — Alembic compares against ``Base.metadata``."""


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class TaskStatus(str, enum.Enum):
    """[FR-02] / SPEC §3 state machine."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    TIMEOUT = "timeout"
    INTERRUPTED = "interrupted"  # [FR-08] graceful-drain overrun


class Scope(str, enum.Enum):
    """[FR-04] scope hierarchy ``read < write < admin``."""

    READ = "read"
    WRITE = "write"
    ADMIN = "admin"


# v2 — many-to-many association table.
task_tags_table = Table(
    "task_tags",
    Base.metadata,
    Column("task_id", String(36), ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Tag(Base):
    """[FR-01] — tag entity, introduced by v2 migration."""

    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("label", name="uq_tags_label"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(64), nullable=False)

    tasks: Mapped[list["Task"]] = relationship(
        secondary=task_tags_table, back_populates="tags", lazy="selectin"
    )


class Task(Base):
    """[FR-01] canonical task record.

    ``result_json`` was created by v1 and removed by v3; the data moves
    into :class:`TaskResult`. ``name`` gains a unique index in v2.
    """

    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("name", name="uq_tasks_name"),  # v2
        Index("ix_tasks_created_at", "created_at"),
        CheckConstraint("length(command) > 0", name="ck_tasks_command_nonempty"),
        CheckConstraint("length(name) > 0 AND length(name) <= 1000", name="ck_tasks_name_len"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    command: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status"), nullable=False, default=TaskStatus.PENDING
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    tags: Mapped[list[Tag]] = relationship(
        secondary=task_tags_table, back_populates="tasks", lazy="selectin"
    )
    runs: Mapped[list["TaskResult"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", lazy="selectin"
    )


class TaskResult(Base):
    """[FR-02] / v3 — execution result split out of ``tasks.result_json``."""

    __tablename__ = "task_results"
    __table_args__ = (
        Index("ix_task_results_task_finished", "task_id", "finished_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(String(36), default=_uuid, nullable=False)
    exit_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    stdout_tail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    stderr_tail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    task: Mapped[Task] = relationship(back_populates="runs")


class APIKey(Base):
    """[FR-03] — API key record. Only ``key_hash`` is stored, never plaintext."""

    __tablename__ = "api_keys"
    __table_args__ = (
        Index("ix_api_keys_scope", "scope"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    scope: Mapped[Scope] = mapped_column(
        Enum(Scope, name="api_key_scope"), nullable=False, default=Scope.READ
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class RateBucket(Base):
    """[FR-05] — per-token token-bucket state in the database."""

    __tablename__ = "rate_buckets"

    key_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("api_keys.id", ondelete="CASCADE"), primary_key=True
    )
    tokens: Mapped[float] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )