"""Alembic environment — wired to taskq_api config [FR-07].

Imports ``Base.metadata`` from the ORM so ``--autogenerate`` produces
SQL aligned with the live schema. The DSN comes from the same
``TASKQ_DB_URL`` env var that the runtime engine uses.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Ensure the source layout is importable when alembic is launched from any cwd.
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "03-development" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from taskq_api.config import get_settings, reset_settings_cache
from taskq_api.models.orm import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject the runtime DB URL so offline + online migrations use the same source.
reset_settings_cache()
settings = get_settings()
# Prefer the URL passed via ``-x sqlalchemy.url=`` (set_main_option) so
# callers (tests, scripts) can override the default without mutating
# environment variables.
explicit_url = config.get_main_option("sqlalchemy.url")
if explicit_url and not explicit_url.endswith("taskq.db"):
    settings_url = explicit_url
else:
    settings_url = settings.taskq_db_url
section = config.get_section(config.config_ini_section, {})
section["sqlalchemy.url"] = settings_url
config.set_main_option("sqlalchemy.url", settings_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout — used by the offline test [FR-07 / NFR-09]."""
    context.configure(
        url=settings_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url_is_sqlite(),
    )
    with context.begin_transaction():
        context.run_migrations()


def url_is_sqlite() -> bool:
    return settings_url.startswith("sqlite")


def run_migrations_online() -> None:
    """Apply migrations against the live database."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        is_sqlite = url_is_sqlite()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()