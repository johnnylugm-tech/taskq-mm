"""v1 — initial tables [FR-07].

Revision ID: v1_initial
Revises:
Create Date: 2026-07-30 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "v1_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("name", sa.String(length=1000), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "done",
                "failed",
                "timeout",
                "interrupted",
                name="task_status",
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.CheckConstraint("length(command) > 0", name="ck_tasks_command_nonempty"),
        sa.CheckConstraint("length(name) > 0 AND length(name) <= 1000", name="ck_tasks_name_len"),
    )
    op.create_index("ix_tasks_created_at", "tasks", ["created_at"])

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column(
            "scope",
            sa.Enum("read", "write", "admin", name="api_key_scope"),
            nullable=False,
            server_default="read",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_api_keys_scope", "api_keys", ["scope"])

    op.create_table(
        "rate_buckets",
        sa.Column(
            "key_id",
            sa.Integer(),
            sa.ForeignKey("api_keys.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("tokens", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("rate_buckets")
    op.drop_index("ix_api_keys_scope", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_index("ix_tasks_created_at", table_name="tasks")
    op.drop_table("tasks")
    sa.Enum(name="api_key_scope").drop(op.get_bind(), checkfirst=False)
    sa.Enum(name="task_status").drop(op.get_bind(), checkfirst=False)