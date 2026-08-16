"""CLI entry point — ``python -m taskq_api`` [SPEC §1].

Subcommands:
- ``migrate up`` / ``migrate down`` — Alembic wrappers.
- ``key create --scope read|write|admin`` — generate a fresh API key.
- ``healthcheck`` — one-shot DB ping.
- ``serve`` — start the ASGI server via uvicorn.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Sequence

from .config import get_settings
from .errors import APIError
from .logging_setup import configure_logging, get_logger
from .models.orm import Scope
from .repository.key_repo import key_repo
from .repository.session import transaction

_logger = get_logger("cli")
_VALID_SCOPES = {s.value for s in Scope}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="taskq-api")
    sub = parser.add_subparsers(dest="command", required=True)

    migrate = sub.add_parser("migrate", help="Run Alembic migrations.")
    migrate.add_argument("direction", choices=["up", "down", "head", "base", "stamp"])
    migrate.add_argument("--revision", default="head")

    key = sub.add_parser("key", help="Manage API keys.")
    key_sub = key.add_subparsers(dest="key_command", required=True)
    key_create = key_sub.add_parser("create", help="Create a new API key.")
    key_create.add_argument("--scope", choices=sorted(_VALID_SCOPES), required=True)

    sub.add_parser("healthcheck", help="Run a one-shot DB ping.")

    serve = sub.add_parser("serve", help="Start the ASGI server via uvicorn.")
    serve.add_argument("--reload", action="store_true")

    initdb = sub.add_parser("initdb", help="Create every table on Base.metadata (tests).")
    return parser


def cmd_migrate(args: argparse.Namespace) -> int:
    from alembic.config import Config
    from alembic import command as alembic_cmd
    config = Config("alembic.ini")
    # ``migrate down`` (no revision) defaults to ``-1`` so a single step
    # rollback stays safe; ``migrate base`` always wipes everything.
    if args.direction in ("up", "head"):
        alembic_cmd.upgrade(config, args.revision)
    elif args.direction == "down":
        alembic_cmd.downgrade(config, args.revision if args.revision != "head" else "-1")
    elif args.direction == "base":
        alembic_cmd.downgrade(config, "base")
    elif args.direction == "stamp":
        alembic_cmd.stamp(config, args.revision)
    return 0


def cmd_key_create(args: argparse.Namespace) -> int:
    scope = Scope(args.scope)
    with transaction() as session:
        row, plaintext = key_repo.create_api_key(session, scope=scope)
    # [FR-03] Print exactly once — never log or persist plaintext.
    sys.stdout.write(f"id={row.id} scope={row.scope.value} key={plaintext}\n")
    sys.stdout.flush()
    return 0


def cmd_healthcheck() -> int:
    from sqlalchemy import text
    with transaction() as session:
        session.execute(text("SELECT 1"))
    sys.stdout.write("healthcheck: ok\n")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn
    settings = get_settings()
    uvicorn.run(
        "taskq_api.app:app",
        host=settings.taskq_host,
        port=settings.taskq_port,
        reload=args.reload,
        log_level=settings.taskq_log_level.lower(),
    )
    return 0


def cmd_initdb(_args: argparse.Namespace | None = None) -> int:
    from .repository.session import create_all
    create_all()
    sys.stdout.write("initdb: created tables\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "migrate":
            return cmd_migrate(args)
        if args.command == "key" and args.key_command == "create":
            return cmd_key_create(args)
        if args.command == "healthcheck":
            return cmd_healthcheck()
        if args.command == "serve":
            return cmd_serve(args)
        if args.command == "initdb":
            return cmd_initdb(args)
    except APIError as exc:
        _logger.error("api error", extra={"detail": str(exc)})
        return 1
    except Exception as exc:  # noqa: BLE001
        _logger.error("cli failure", extra={"error_type": type(exc).__name__})
        return 2
    parser.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())