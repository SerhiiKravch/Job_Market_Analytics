"""Alembic uses the application's connection and transaction settings."""

from pathlib import Path

from alembic import context

from job_market.config import load_settings
from job_market.storage.database import create_database_engine
from job_market.storage.models import Base

config = context.config


def _database_url() -> str:
    if "database_url" in config.attributes:
        return config.attributes["database_url"]
    root = Path(config.config_file_name).resolve().parent if config.config_file_name else None
    return load_settings(root).database_url.get_secret_value()


def _migrate(connection) -> None:
    sqlite = connection.dialect.name == "sqlite"
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        compare_server_default=True,
        render_as_batch=sqlite,
        transactional_ddl=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(
        url=_database_url(),
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
elif "connection" in config.attributes:
    _migrate(config.attributes["connection"])
else:
    engine = create_database_engine(_database_url())
    try:
        with engine.begin() as connection:
            _migrate(connection)
    finally:
        engine.dispose()
