"""Keep tests independent of the operator's actual configuration."""

import os

import pytest

_SETTING_NAMES = {
    "project_root",
    "telegram_api_id",
    "telegram_api_hash",
    "telegram_channel",
    "telegram_session_path",
    "database_url",
    "backfill_days",
    "report_output_dir",
    "report_timezone",
    "log_level",
    "pydantic_settings_debug",
}


@pytest.fixture(autouse=True)
def isolated_settings_environment(monkeypatch):
    for name in tuple(os.environ):
        if name.lower() in _SETTING_NAMES:
            monkeypatch.delenv(name)
