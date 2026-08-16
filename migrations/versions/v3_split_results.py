"""v3 — split task_results out of tasks.result_json with full data migration [FR-07].

Revision ID: v3_split_results
Revises: v2_tags
Create Date: 2026-07-30 00:02:00.000000

This is the high-risk revision. Each existing row in ``tasks.result_json``
is migrated into a fresh ``task_results`` row, preserving every column.
The downgrade reverses the migration so the round-trip acceptance test
(SPEC §8 #12) can prove column-by-column equivalence.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "v3_split_results"
down_revision: Union[str, None] = "v2_tags"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE_NAME = "task_results"


def upgrade() -> None:
    # 1. Create the new table.
    op.create_table(
        _TABLE_NAME,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "task_id",
            sa.String(length=36),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("stdout_tail", sa.Text(), nullable=True),
        sa.Column("stderr_tail", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_task_results_task_finished", _TABLE_NAME, ["task_id", "finished_at"]
    )

    # 2. Migrate every existing result_json blob into task_results.
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, result_json, updated_at FROM tasks")).fetchall()
    now = datetime.now(tz=timezone.utc)
    for row in rows:
        blob = row.result_json
        if not blob:
            continue
        # ``blob`` may come back as a dict (sqlite/mysql) or a JSON string.
        if isinstance(blob, str):
            import json

            blob = json.loads(blob)
        exit_code = blob.get("exit_code")
        stdout_tail = blob.get("stdout_tail")
        stderr_tail = blob.get("stderr_tail")
        duration_ms = blob.get("duration_ms")
        bind.execute(
            sa.text(
                "INSERT INTO task_results "
                "(task_id, run_id, exit_code, stdout_tail, stderr_tail, duration_ms, started_at, finished_at) "
                "VALUES (:task_id, :run_id, :exit_code, :stdout_tail, :stderr_tail, :duration_ms, :started_at, :finished_at)"
            ),
            {
                "task_id": row.id,
                "run_id": str(uuid.uuid4()),
                "exit_code": exit_code,
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
                "duration_ms": duration_ms,
                "started_at": now,
                "finished_at": now,
            },
        )

    # 3. Drop the legacy column. SQLite requires batch mode for column drops;
    # the env.py already sets render_as_batch=True on sqlite URLs.
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("result_json")


def downgrade() -> None:
    # 1. Re-add result_json with the JSON type.
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("result_json", sa.JSON(), nullable=True))

    # 2. Reverse-migrate the rows back into tasks.result_json.
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT task_id, exit_code, stdout_tail, stderr_tail, duration_ms "
            "FROM task_results ORDER BY id ASC"
        )
    ).fetchall()
    # Group by task_id — keep the *first* (oldest) result per task when
    # rolling back, since the v1 schema only allowed one.
    seen: set[str] = set()
    payloads: list[tuple[str, dict]] = []
    for row in rows:
        if row.task_id in seen:
            continue
        seen.add(row.task_id)
        blob = {
            "exit_code": row.exit_code,
            "stdout_tail": row.stdout_tail,
            "stderr_tail": row.stderr_tail,
            "duration_ms": row.duration_ms,
        }
        payloads.append((row.task_id, blob))
    for task_id, blob in payloads:
        bind.execute(
            sa.text("UPDATE tasks SET result_json = :blob WHERE id = :id"),
            {"id": task_id, "blob": _json_dumps(blob)},
        )

    # 3. Drop the task_results table.
    op.drop_index("ix_task_results_task_finished", table_name=_TABLE_NAME)
    op.drop_table(_TABLE_NAME)


def _json_dumps(obj: dict) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)