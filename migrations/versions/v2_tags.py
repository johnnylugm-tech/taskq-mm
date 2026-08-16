"""v2 — tags + many-to-many + unique task name [FR-07].

Revision ID: v2_tags
Revises: v1_initial
Create Date: 2026-07-30 00:01:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "v2_tags"
down_revision: Union[str, None] = "v1_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tags",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("label", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("label", name="uq_tags_label"),
    )
    op.create_table(
        "task_tags",
        sa.Column(
            "task_id",
            sa.String(length=36),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "tag_id",
            sa.Integer(),
            sa.ForeignKey("tags.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    # SQLite requires batch mode for ADD CONSTRAINT — env.py sets
    # render_as_batch=True on sqlite URLs.
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.create_unique_constraint("uq_tasks_name", ["name"])


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_constraint("uq_tasks_name", type_="unique")
    op.drop_table("task_tags")
    op.drop_table("tags")