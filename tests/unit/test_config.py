"""Offline configuration behavior and privacy boundaries."""

from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from job_market.config import ConfigurationError, load_settings


def test_defaults_resolve_against_working_directory_without_creating_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = load_settings()

    assert settings.project_root == tmp_path
    assert settings.telegram_session_path == tmp_path / "var/telegram.session"
    assert settings.report_output_dir == tmp_path / "var/reports"
    assert make_url(settings.database_url.get_secret_value()).database == str(
        tmp_path / "var/jobs.db"
    )
    assert settings.backfill_days == 30
    assert settings.report_timezone == "Europe/Warsaw"
    assert settings.log_level == "INFO"
    assert list(tmp_path.iterdir()) == []


def test_explicit_root_loads_only_its_dotenv_and_environment_wins(tmp_path, monkeypatch):
    project = tmp_path / "project"
    elsewhere = tmp_path / "elsewhere"
    project.mkdir()
    elsewhere.mkdir()
    (project / ".env").write_text(
        "BACKFILL_DAYS=12\nREPORT_OUTPUT_DIR=exports\nTELEGRAM_SESSION_PATH=sessions/test.session\n"
        "DATABASE_URL=sqlite:///data/jobs.db\nLOG_LEVEL=warning\n"
        "PROJECT_ROOT=/ignored/from/dotenv\nUNRELATED_SETTING=ignored\n",
        encoding="utf-8",
    )
    (elsewhere / ".env").write_text("BACKFILL_DAYS=3\n", encoding="utf-8")
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("BACKFILL_DAYS", "7")
    monkeypatch.setenv("PROJECT_ROOT", "/ignored/from/environment")

    settings = load_settings(project)

    assert settings.project_root == project
    assert settings.backfill_days == 7
    assert settings.report_output_dir == project / "exports"
    assert settings.telegram_session_path == project / "sessions/test.session"
    assert make_url(settings.database_url.get_secret_value()).database == str(
        project / "data/jobs.db"
    )
    assert settings.log_level == "WARNING"


def test_relative_and_absolute_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPORT_OUTPUT_DIR", str(tmp_path / "absolute/reports"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/absolute/jobs.db")
    settings = load_settings(Path("."))
    assert settings.report_output_dir == tmp_path / "absolute/reports"
    assert settings.database_url.get_secret_value() == f"sqlite:///{tmp_path}/absolute/jobs.db"


def test_home_paths_are_expanded_before_root_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_SESSION_PATH", "~/job-market-test/session")
    monkeypatch.setenv("REPORT_OUTPUT_DIR", "~/job-market-test/reports")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///~/job-market-test/jobs.db")
    settings = load_settings(tmp_path)
    expected = Path.home() / "job-market-test"
    assert settings.telegram_session_path == expected / "session"
    assert settings.report_output_dir == expected / "reports"
    assert make_url(settings.database_url.get_secret_value()).database == str(expected / "jobs.db")


def test_in_memory_sqlite_is_not_resolved_as_a_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    assert load_settings(tmp_path).database_url.get_secret_value() == "sqlite:///:memory:"


def test_postgresql_password_is_preserved_without_being_exposed(tmp_path, monkeypatch):
    url = "postgresql+psycopg://operator:synthetic-password@localhost/jobs"
    monkeypatch.setenv("DATABASE_URL", url)
    settings = load_settings(tmp_path)
    assert settings.database_url.get_secret_value() == url
    assert "synthetic-password" not in repr(settings)
    assert "synthetic-password" not in settings.model_dump_json()


def test_example_placeholders_work_offline_and_live_validation_is_explicit(tmp_path):
    example = Path(__file__).resolve().parents[2] / ".env.example"
    (tmp_path / ".env").write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    settings = load_settings(tmp_path)
    assert settings.backfill_days == 30
    with pytest.raises(ConfigurationError, match="TELEGRAM_API_ID"):
        settings.telegram_credentials()
    with pytest.raises(ConfigurationError, match="TELEGRAM_CHANNEL"):
        settings.require_channel()


def test_missing_credentials_do_not_block_offline_settings(tmp_path):
    settings = load_settings(tmp_path)
    assert settings.telegram_api_id is None
    with pytest.raises(ConfigurationError, match="TELEGRAM_API_ID"):
        settings.telegram_credentials()


def test_explicit_telegram_validation_without_connecting(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "a" * 32)
    monkeypatch.setenv("TELEGRAM_CHANNEL", "@synthetic_fixture_channel")
    settings = load_settings(tmp_path)
    credentials = settings.telegram_credentials()
    assert credentials.api_id == 12345
    assert credentials.api_hash.get_secret_value() == "a" * 32
    assert settings.require_channel() == "@synthetic_fixture_channel"
    for rendered in (repr(settings), settings.model_dump_json(), repr(credentials)):
        assert "12345" not in rendered
        assert "a" * 32 not in rendered
        assert "@synthetic_fixture_channel" not in rendered


@pytest.mark.parametrize("api_id", ["0", "-1", "2147483648", "not-numeric", "9" * 100])
def test_invalid_api_ids_fail_only_explicit_telegram_validation(tmp_path, monkeypatch, api_id):
    monkeypatch.setenv("TELEGRAM_API_ID", api_id)
    with pytest.raises(ConfigurationError, match="TELEGRAM_API_ID"):
        load_settings(tmp_path).telegram_credentials()


def test_invalid_hash_error_contains_no_value(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "synthetic-secret-invalid-hash")
    with pytest.raises(ConfigurationError) as exc:
        load_settings(tmp_path).telegram_credentials()
    assert "TELEGRAM_API_HASH" in str(exc.value)
    assert "synthetic-secret" not in str(exc.value)


@pytest.mark.parametrize(
    ("name", "value", "guidance"),
    [
        ("BACKFILL_DAYS", "0", "BACKFILL_DAYS"),
        ("BACKFILL_DAYS", "31", "BACKFILL_DAYS"),
        ("BACKFILL_DAYS", "synthetic-secret", "BACKFILL_DAYS"),
        ("REPORT_TIMEZONE", "synthetic-secret", "REPORT_TIMEZONE"),
        ("REPORT_TIMEZONE", "../synthetic-secret", "REPORT_TIMEZONE"),
        ("LOG_LEVEL", "synthetic-secret", "LOG_LEVEL"),
        ("DATABASE_URL", "synthetic-secret", "DATABASE_URL"),
        ("DATABASE_URL", "mysql://user:synthetic-secret@localhost/db", "DATABASE_URL"),
        ("DATABASE_URL", "postgresql://user:synthetic-secret@localhost:bad/db", "DATABASE_URL"),
        ("DATABASE_URL", "sqlite:///file:synthetic-secret?uri=true", "DATABASE_URL"),
        ("DATABASE_URL", "sqlite+aiosqlite:///synthetic-secret.db", "DATABASE_URL"),
        ("DATABASE_URL", "sqlite://", "DATABASE_URL"),
    ],
)
def test_settings_errors_are_actionable_without_echoing_values(
    tmp_path, monkeypatch, name, value, guidance
):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError) as exc:
        load_settings(tmp_path)
    assert guidance in str(exc.value)
    assert "synthetic-secret" not in str(exc.value)
    assert "input_value" not in str(exc.value)


@pytest.mark.parametrize("name", ["TELEGRAM_SESSION_PATH", "DATABASE_URL", "REPORT_OUTPUT_DIR"])
def test_file_directory_mismatch_is_rejected(tmp_path, monkeypatch, name):
    file = tmp_path / "file"
    file.touch()
    value = str(file) if name == "REPORT_OUTPUT_DIR" else str(tmp_path)
    if name == "DATABASE_URL":
        value = f"sqlite:///{value}"
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError, match=name):
        load_settings(tmp_path)


def test_missing_project_root_fails_clearly(tmp_path):
    with pytest.raises(ConfigurationError, match="--project-root"):
        load_settings(tmp_path / "missing")


def test_non_utf8_dotenv_error_does_not_echo_content(tmp_path):
    (tmp_path / ".env").write_bytes(b"TELEGRAM_API_HASH=synthetic-secret\xff")
    with pytest.raises(ConfigurationError, match="Cannot load project settings") as exc:
        load_settings(tmp_path)
    assert "synthetic-secret" not in str(exc.value)
