"""Protect runtime and planning files without creating any private artifacts."""

import subprocess
from pathlib import Path


def test_private_runtime_paths_and_local_plan_are_ignored():
    root = Path(__file__).resolve().parents[2]
    paths = [
        "MVP_IMPLEMENTATION_PLAN.md",
        ".env",
        ".env.local",
        ".env.production",
        "var/reports/output.csv",
        "var/reviews/edit.json",
        "local.session",
        "local.session-journal",
        "local.session-wal",
        "local.session.backup",
        "local.db",
        "local.db-wal",
        "local.db-shm",
        "local.sqlite",
        "local.sqlite3",
        "local.sqlite3-journal",
        "application.log",
        ".venv/pyvenv.cfg",
    ]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        input="\n".join(paths) + "\n",
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert set(result.stdout.splitlines()) == set(paths)
    example = subprocess.run(
        ["git", "check-ignore", "--no-index", ".env.example"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert example.returncode == 1
