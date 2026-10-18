"""Exercise the installed entry points in a separate, offline process."""

import json
import subprocess
import sys
from pathlib import Path

import pytest


def run_cli(tmp_path, *arguments):
    return subprocess.run(
        [sys.executable, "-m", "job_market", *arguments],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


@pytest.mark.parametrize("arguments", [(), ("--help",), ("--version",), ("config", "--help")])
def test_help_and_version_do_not_load_settings(tmp_path, arguments):
    (tmp_path / ".env").write_text("DATABASE_URL=invalid-secret\n", encoding="utf-8")
    result = run_cli(tmp_path, *arguments)
    assert result.returncode == 0
    assert "job-market" in result.stdout
    assert "invalid-secret" not in result.stdout + result.stderr
    assert sorted(path.name for path in tmp_path.iterdir()) == [".env"]


def test_console_script_entry_point(tmp_path):
    executable = Path(sys.executable).parent / "job-market"
    result = subprocess.run(
        [str(executable), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0
    assert "--project-root" in result.stdout


def test_offline_check_needs_no_credentials_and_creates_no_runtime_files(tmp_path):
    result = run_cli(tmp_path, "config", "check")
    assert result.returncode == 0
    assert "offline settings only" in result.stdout
    assert json.loads(result.stderr)["event"] == "configuration_validated"
    assert list(tmp_path.iterdir()) == []


def test_explicit_project_root_is_used_from_elsewhere(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("BACKFILL_DAYS=31\n", encoding="utf-8")
    result = run_cli(tmp_path, "--project-root", str(project), "config", "check")
    assert result.returncode == 2
    assert "BACKFILL_DAYS" in result.stderr


def test_live_configuration_check_fails_clearly_without_credentials(tmp_path):
    result = run_cli(tmp_path, "config", "check", "--telegram")
    assert result.returncode == 2
    assert "TELEGRAM_API_ID" in result.stderr
    assert "Traceback" not in result.stderr


def test_credential_validation_is_local_only(tmp_path):
    (tmp_path / ".env").write_text(
        f"TELEGRAM_API_ID=12345\nTELEGRAM_API_HASH={'a' * 32}\n"
        "TELEGRAM_CHANNEL=@synthetic_fixture_channel\n",
        encoding="utf-8",
    )
    result = run_cli(tmp_path, "config", "check", "--telegram")
    assert result.returncode == 0
    assert "No network connection was made" in result.stdout
    for secret in ("12345", "a" * 32, "@synthetic_fixture_channel"):
        assert secret not in result.stdout + result.stderr
    assert sorted(path.name for path in tmp_path.iterdir()) == [".env"]


def test_invalid_setting_does_not_echo_secret_or_traceback(tmp_path):
    (tmp_path / ".env").write_text(
        "DATABASE_URL=synthetic-secret\nTELEGRAM_API_HASH=another-synthetic-secret\n",
        encoding="utf-8",
    )
    result = run_cli(tmp_path, "config", "check")
    assert result.returncode == 2
    assert "DATABASE_URL" in result.stderr
    assert "synthetic-secret" not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


def test_imports_have_no_runtime_or_network_side_effects(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import logging
import sys
from pathlib import Path

def forbid_network(event, args):
    if event.startswith('socket.'):
        raise AssertionError("unexpected network initialization")

sys.addaudithook(forbid_network)
import job_market
import job_market.config
import job_market.logging_config
import job_market.cli
assert not logging.getLogger().handlers
assert not list(Path.cwd().iterdir())
assert 'telethon' not in __import__('sys').modules
""",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_configuration_check_does_not_open_network_socket(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
def forbid_network(event, args):
    if event.startswith('socket.'):
        raise AssertionError("unexpected network access")
sys.addaudithook(forbid_network)
from job_market.cli import main
raise SystemExit(main(['config', 'check']))
""",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_settings_debug_mode_cannot_expose_configuration_values(tmp_path, monkeypatch):
    monkeypatch.setenv("PYDANTIC_SETTINGS_DEBUG", "1")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("TELEGRAM_API_HASH", "synthetic-private-hash")
    result = run_cli(tmp_path, "config", "check")
    assert result.returncode == 0
    assert "synthetic-private-hash" not in result.stdout + result.stderr
    for line in result.stderr.splitlines():
        assert json.loads(line)["event"] in {
            "unstructured_log_redacted",
            "configuration_validated",
        }
