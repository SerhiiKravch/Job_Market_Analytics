"""Run bundled Alembic revisions with the same engine policy as storage."""

from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import as_file, files
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.util import CommandError
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from job_market.storage.database import create_database_engine


class MigrationError(ValueError):
    """An operator-facing migration error with no credentials or SQL text."""


@contextmanager
def migration_config() -> Iterator[Config]:
    bundled = files("job_market.storage").joinpath("migrations")
    # Editable installs use the same revision source that is bundled in wheels.
    directory = bundled if bundled.is_dir() else Path(__file__).resolve().parents[3] / "migrations"
    if not directory.is_dir():
        raise MigrationError("Migration files are missing; reinstall the package.")
    with as_file(directory) as path:
        config = Config()
        config.set_main_option("script_location", str(path).replace("%", "%%"))
        yield config


def upgrade_database(database_url: str) -> None:
    engine = None
    try:
        url = make_url(database_url)
        if url.get_backend_name() == "sqlite":
            if url.database in (None, "", ":memory:"):
                raise MigrationError(
                    "Set DATABASE_URL to a file-backed SQLite database to upgrade."
                )
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
        engine = create_database_engine(url)
        with migration_config() as config, engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    except ImportError:
        raise MigrationError(
            "The database driver is unavailable; use the local SQLite URL."
        ) from None
    except (OSError, SQLAlchemyError, CommandError, RuntimeError):
        raise MigrationError(
            "Could not upgrade the database. Check the configured database path, permissions, "
            "and whether another process is using it."
        ) from None
    finally:
        if engine is not None:
            engine.dispose()
