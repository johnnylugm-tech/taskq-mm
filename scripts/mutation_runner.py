"""Lightweight mutation runner — apply targeted source mutations, then
run the unit tests. Counts kills / survivors and reports the score.

Each entry in :data:`MUTATIONS` describes one targeted edit that flips a
control-flow decision or a comparison operator. The runner applies the
edit, runs the unit suite, then reverts the edit before reporting.

Score = killed / (killed + survived). Threshold: 70.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(cmd: list[str], env: dict[str, str], cwd: str) -> int:
    return subprocess.call(cmd, env=env, cwd=cwd)


def run_pytest(tests_dir: str) -> int:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "03-development" / "src") + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        tests_dir,
        "-x",
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
    ]
    return _run(cmd, env, str(ROOT))


def edit_and_run(rel_path: str, fn_name: str, edit_fn, test_dir: str) -> bool:
    target = ROOT / "03-development" / "src" / "taskq_api" / rel_path
    original = target.read_text()
    new_text = edit_fn(original)
    if new_text == original:
        return False
    target.write_text(new_text)
    try:
        rc = run_pytest(test_dir)
        return rc != 0  # killed if pytest failed
    finally:
        target.write_text(original)


def flip_scope_check(text: str) -> str:
    """Flip ``>=`` to ``>`` inside ``scope_satisfies`` to make admin-downgrade wrong."""
    return text.replace(
        "return _SCOPE_ORDER[actual] >= _SCOPE_ORDER[required]",
        "return _SCOPE_ORDER[actual] > _SCOPE_ORDER[required]",
        1,
    )


def break_rate_limit_retry(text: str) -> str:
    """Remove the rate-limit rejection so over-budget requests succeed."""
    return text.replace(
        "        raise RateLimitedError(",
        "        return  # mutation: never raise\n        raise RateLimitedError(",
        1,
    )


def break_kill_orphan(text: str) -> str:
    """Disable the timeout kill — orphans survive."""
    return text.replace(
        "    try:\n        proc.kill()",
        "    try:\n        pass  # mutation: do not kill",
        1,
    )


def break_delete_task(text: str) -> str:
    """Replace DELETE with GET-equivalent — never raises NotFound."""
    return text.replace(
        "    removed = task_repo.delete_task(session, task_id)",
        "    removed = task_repo.create_task(session, command='x', name='x')",
        1,
    )


def drop_selectinload(text: str) -> str:
    """N+1 reintroduced."""
    return text.replace(
        "        select(Task).where(Task.id == task_id).options(selectinload(Task.tags))",
        "        select(Task).where(Task.id == task_id)",
        1,
    )


def weaken_hash(text: str) -> str:
    """Use md5 instead of sha256 — 32 hex chars vs 64."""
    return text.replace(
        "return hashlib.sha256(plaintext.encode(\"utf-8\")).hexdigest()",
        "import hashlib as _h\n    return _h.md5(plaintext.encode(\"utf-8\")).hexdigest()",
        1,
    )


def break_row_lock(text: str) -> str:
    """Skip the row-level FOR UPDATE — race over-admission."""
    return text.replace(
        "select_for_update(session, RateBucket)",
        "select(RateBucket)",
        1,
    )


def skip_rollback(text: str) -> str:
    """Drop the rollback path so a failed transaction commits dirty state."""
    return text.replace(
        "    except Exception:\n        session.rollback()\n        raise",
        "    except Exception:\n        raise",
        1,
    )


def flip_status_done(text: str) -> str:
    """In ``_derive_status``, return DONE for exit_code=1 — wrong status."""
    return text.replace(
        "return TaskStatus.DONE if exit_code == 0 else TaskStatus.FAILED",
        "return TaskStatus.DONE",
        1,
    )


MUTATIONS: list[tuple[str, str, callable, str, str]] = [
    ("service/auth.py",            "scope_satisfies",   flip_scope_check,     "tests/unit/test_service.py",    "scope hierarchy broken"),
    ("service/ratelimit.py",       "consume_or_raise",  break_rate_limit_retry,"tests/unit/test_service.py",   "rate limit never rejects"),
    ("service/runner.py",          "_kill_and_wait",    break_kill_orphan,    "tests/unit/test_runner.py",     "timeout leaves orphan process"),
    ("service/tasks.py",           "delete_task",       break_delete_task,    "tests/unit/test_service.py",    "delete swallows existence"),
    ("repository/task_repo.py",     "find_task_by_id",   drop_selectinload,    "tests/unit/test_repository.py", "N+1 reintroduced"),
    ("repository/key_repo.py",      "create_api_key",    weaken_hash,          "tests/unit/test_repository.py", "API key hashed with md5 (32 hex)"),
    ("repository/rate_repo.py",     "take_token",        break_row_lock,       "tests/unit/test_repository.py", "row lock dropped"),
    ("repository/session.py",      "transaction",       skip_rollback,        "tests/unit/test_repository.py", "transaction never rolls back"),
    ("service/tasks.py",           "_derive_status",    flip_status_done,     "tests/unit/test_service.py",    "non-zero exit mapped to DONE"),
]


def main() -> int:
    print(f"Mutation runner — scope: service/ + repository/")
    killed: list[str] = []
    survived: list[str] = []
    no_apply: list[str] = []
    for rel, fn_name, edit_fn, test_dir, desc in MUTATIONS:
        killed_flag = edit_and_run(rel, fn_name, edit_fn, test_dir)
        if killed_flag is False and not _verify_edit_applied(rel, edit_fn):
            no_apply.append(f"{rel}::{fn_name}  ({desc})")
            continue
        if killed_flag:
            killed.append(f"{rel}::{fn_name}  ({desc})")
        else:
            survived.append(f"{rel}::{fn_name}  ({desc})")
    total = len(killed) + len(survived)
    score = 100.0 * len(killed) / total if total else 0.0
    print("")
    print(textwrap.dedent(f"""
        === Mutation score ===
        killed:    {len(killed)}
        survived:  {len(survived)}
        no_apply:  {len(no_apply)}
        score:     {score:.1f}%   (threshold: 70%)
        verdict:   {'PASS' if score >= 70 else 'FAIL'}
    """))
    return 0 if score >= 70 else 1


def _verify_edit_applied(rel: str, edit_fn) -> bool:
    target = ROOT / "03-development" / "src" / "taskq_api" / rel
    original = target.read_text()
    return edit_fn(original) != original


if __name__ == "__main__":
    raise SystemExit(main())