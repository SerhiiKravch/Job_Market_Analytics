"""Validated local settings. Loading configuration creates no runtime resources."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, ValidationError, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class ConfigurationError(ValueError):
    """An actionable error whose message contains no configuration values."""


@dataclass(frozen=True)
class TelegramCredentials:
    api_id: int = field(repr=False)
    api_hash: SecretStr = field(repr=False)


class Settings(BaseSettings):
    """Use :func:`load_settings` to obtain sanitized operator-facing errors."""

    model_config = SettingsConfigDict(
        env_file=None,
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
        frozen=True,
    )

    # Init values take precedence over environment variables and dotenv values.
    project_root: Path
    telegram_api_id: SecretStr | None = Field(default=None, repr=False, exclude=True)
    telegram_api_hash: SecretStr | None = Field(default=None, repr=False, exclude=True)
    telegram_channel: str | None = Field(default=None, repr=False, exclude=True)
    telegram_session_path: Path = Field(default=Path("var/telegram.session"), repr=False)
    database_url: SecretStr = Field(
        default=SecretStr("sqlite:///./var/jobs.db"), repr=False, exclude=True
    )
    backfill_days: int = Field(default=30, ge=1, le=30)
    report_output_dir: Path = Path("var/reports")
    report_timezone: str = "Europe/Warsaw"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("telegram_session_path", "report_output_dir")
    @classmethod
    def resolve_path(cls, value: Path, info: ValidationInfo) -> Path:
        resolved = (info.data["project_root"] / value.expanduser()).resolve()
        if info.field_name == "telegram_session_path" and resolved.is_dir():
            raise ValueError("Expected a session file path")
        if info.field_name == "report_output_dir" and resolved.exists() and not resolved.is_dir():
            raise ValueError("Expected a report directory path")
        return resolved

    @field_validator("database_url")
    @classmethod
    def resolve_database_url(cls, value: SecretStr, info: ValidationInfo) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
            if url.get_backend_name() not in {"sqlite", "postgresql"}:
                raise ValueError("Unsupported database backend")
            # Port access also validates malformed ports without exposing their value.
            _ = url.port
            if url.get_backend_name() == "sqlite":
                if url.drivername != "sqlite" or url.host or url.username or url.password:
                    raise ValueError("Use a local synchronous SQLite URL")
                if not url.database or url.query:
                    raise ValueError("Specify a SQLite path or :memory:, without URI parameters")
                if url.database != ":memory:":
                    path = (info.data["project_root"] / Path(url.database).expanduser()).resolve()
                    if path.is_dir():
                        raise ValueError("Expected a database file path")
                    url = url.set(database=str(path))
            elif not url.database:
                raise ValueError("Specify a PostgreSQL database name")
            return SecretStr(url.render_as_string(hide_password=False))
        except (ArgumentError, ValueError, OSError) as exc:
            raise ValueError("Invalid database URL") from exc

    @field_validator("report_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Expected an IANA timezone name") from exc
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    def telegram_credentials(self) -> TelegramCredentials:
        """Validate credentials only at an explicit Telegram command boundary."""
        raw_id = self.telegram_api_id.get_secret_value() if self.telegram_api_id else ""
        raw_hash = self.telegram_api_hash.get_secret_value() if self.telegram_api_hash else ""
        if not re.fullmatch(r"[0-9]{1,10}", raw_id) or not 0 < int(raw_id) < 2**31:
            raise ConfigurationError("Set TELEGRAM_API_ID to your positive numeric API ID.")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", raw_hash):
            raise ConfigurationError("Set TELEGRAM_API_HASH to your 32-character API hash.")
        return TelegramCredentials(api_id=int(raw_id), api_hash=SecretStr(raw_hash))

    def require_channel(self) -> str:
        """Return the configured reference; entity resolution belongs to ingestion."""
        channel = (self.telegram_channel or "").strip()
        if not channel or channel == "@your_channel_username":
            raise ConfigurationError("Set TELEGRAM_CHANNEL to an already accessible channel.")
        return channel


_SETTING_GUIDANCE = {
    "telegram_session_path": "TELEGRAM_SESSION_PATH must identify a session file path.",
    "database_url": (
        "DATABASE_URL must be a valid SQLite file URL (sqlite:///./var/jobs.db), "
        "sqlite:///:memory:, or PostgreSQL URL; SQLite URI parameters are unsupported."
    ),
    "backfill_days": "BACKFILL_DAYS must be an integer from 1 through 30.",
    "report_output_dir": "REPORT_OUTPUT_DIR must identify a directory path.",
    "report_timezone": "REPORT_TIMEZONE must be an IANA timezone such as Europe/Warsaw.",
    "log_level": "LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL.",
}


def load_settings(project_root: Path | str | None = None) -> Settings:
    """Resolve the root, then load its .env with real environment values taking priority."""
    try:
        root = (Path.cwd() if project_root is None else Path(project_root)).expanduser().resolve()
        if not root.is_dir():
            raise ConfigurationError("--project-root must identify an existing directory.")
        return Settings(project_root=root, _env_file=root / ".env")
    except ValidationError as exc:
        # Do not render Pydantic input, context, or raw validator messages; any of
        # those may include secrets or other private operator-supplied content.
        guidance = dict.fromkeys(
            _SETTING_GUIDANCE.get(str(error["loc"][0]), "Check the named settings in .env.")
            for error in exc.errors(include_input=False, include_context=False)
        )
        raise ConfigurationError(" ".join(guidance)) from None
    except (OSError, UnicodeError, SettingsError) as exc:
        raise ConfigurationError(
            "Cannot load project settings; check .env and path permissions."
        ) from exc
