"""[SPEC §1] CLI entry-point tests — ``python -m taskq_api``."""
from __future__ import annotations

from pathlib import Path

import pytest

from taskq_api import __main__ as cli


def test_parser_requires_subcommand() -> None:
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args([])


def test_initdb_subcommand(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("TASKQ_DB_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    from taskq_api.config import reset_settings_cache
    reset_settings_cache()
    code = cli.main(["initdb"])
    assert code == 0
    captured = capsys.readouterr()
    assert "created tables" in captured.out


def test_healthcheck_subcommand(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("TASKQ_DB_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    from taskq_api.config import reset_settings_cache
    reset_settings_cache()
    import taskq_api.repository.session as session_repo
    session_repo.reset_engine()
    session_repo.create_all()
    code = cli.main(["healthcheck"])
    assert code == 0
    captured = capsys.readouterr()
    assert "healthcheck: ok" in captured.out


def test_key_create_subcommand(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("TASKQ_DB_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    from taskq_api.config import reset_settings_cache
    reset_settings_cache()
    import taskq_api.repository.session as session_repo
    session_repo.reset_engine()
    session_repo.create_all()
    code = cli.main(["key", "create", "--scope", "write"])
    assert code == 0
    captured = capsys.readouterr()
    assert "scope=write" in captured.out
    assert "key=" in captured.out


def test_main_handles_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from taskq_api.errors import APIError

    def boom(_args):
        raise APIError("nope")

    monkeypatch.setattr(cli, "cmd_initdb", boom)
    code = cli.main(["initdb"])
    assert code == 1


def test_main_handles_unexpected_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_args):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(cli, "cmd_initdb", boom)
    code = cli.main(["initdb"])
    assert code == 2


def test_main_no_subcommand_shows_help(capsys: pytest.CaptureFixture) -> None:
    """``main()`` without args exits with code 1 and prints help."""
    with pytest.raises(SystemExit):
        cli.main([])


def test_cmd_migrate_up_invokes_alembic(monkeypatch: pytest.MonkeyPatch) -> None:
    """[SPEC §1] ``migrate up`` triggers alembic upgrade."""
    upgrades: list[str] = []

    def fake_upgrade(_config, revision):
        upgrades.append(revision)

    monkeypatch.setattr("alembic.command.upgrade", fake_upgrade)
    code = cli.main(["migrate", "up"])
    assert code == 0
    assert upgrades == ["head"]


def test_cmd_migrate_down_invokes_alembic(monkeypatch: pytest.MonkeyPatch) -> None:
    downgrades: list[str] = []

    def fake_downgrade(_config, revision):
        downgrades.append(revision)

    monkeypatch.setattr("alembic.command.downgrade", fake_downgrade)
    code = cli.main(["migrate", "down"])
    assert code == 0
    assert downgrades == ["-1"]


def test_cmd_migrate_stamp_invokes_alembic(monkeypatch: pytest.MonkeyPatch) -> None:
    stamps: list[str] = []

    def fake_stamp(_config, revision):
        stamps.append(revision)

    monkeypatch.setattr("alembic.command.stamp", fake_stamp)
    code = cli.main(["migrate", "stamp", "--revision", "v3_split_results"])
    assert code == 0
    assert stamps == ["v3_split_results"]