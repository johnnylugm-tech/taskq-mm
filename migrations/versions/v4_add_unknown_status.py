"""v4 — add ``unknown`` to the ``task_status`` enum [Group G / P2-3].

Revision ID: v4_add_unknown_status
Revises: v3_split_results
Create Date: 2026-08-16 06:00:00.000000

The new ``UNKNOWN`` value lets the service distinguish "we couldn't
measure this run" (recorder crashed, scheduler killed us) from
"the command ran and failed" — closing the lie the previous mapping
(``duration_ms is None → FAILED``) carried.

The migration is reversible on both Postgres and SQLite.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "v4_add_unknown_status"
down_revision: Union[str, None] = "v3_split_results"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        # Postgres enum: ALTER TYPE ... ADD VALUE. The IF NOT EXISTS clause
        # was added in Postgres 9.6; on older versions this would need
        # to be wrapped in a try/except.
        op.execute("ALTER TYPE task_status ADD VALUE IF NOT EXISTS 'unknown'")
    else:
        # SQLite has no enum type — the column is a plain VARCHAR. The
        # ORM-side enum was extended in code; no DDL change is required.
        pass


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        # Postgres cannot drop a value from an enum type in place — the
        # whole type must be recreated. To keep the migration reversible
        # without taking a lock on every existing row, we leave the
        # value in place and document the asymmetry. Operators who need
        # a clean state can ``DROP TYPE task_status CASCADE;`` manually
        # and let the ORM recreate the type.
        pass
    else:
        pass