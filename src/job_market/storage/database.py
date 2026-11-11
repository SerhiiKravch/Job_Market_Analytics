"""Engine creation and serialized, short-lived storage transactions."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import Lock

from sqlalchemy import BigInteger, DateTime, Integer, create_engine, event
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from job_market.domain.validation import as_utc

BUSY_TIMEOUT_MS = 5000
InternalId = BigInteger().with_variant(Integer(), "sqlite")


class UtcDateTime(TypeDecorator[datetime]):
    """SQLite stores naive UTC; PostgreSQL uses TIMESTAMP WITH TIME ZONE."""

    impl = DateTime(timezone=True).with_variant(DateTime(timezone=False), "sqlite")
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        utc = as_utc(value)
        return utc.replace(tzinfo=None) if dialect.name == "sqlite" else utc

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "sqlite":
            return value.replace(tzinfo=UTC)
        return as_utc(value)


def create_database_engine(database_url: str | URL) -> Engine:
    """Configure connections without opening one or writing files on creation."""
    url = make_url(database_url)
    engine = create_engine(url, hide_parameters=True)
    if url.get_backend_name() == "sqlite":
        file_database = url.database not in (None, "", ":memory:")

        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection, _record):
            # SQLAlchemy controls BEGIN, including for reads and DDL. These
            # pragmas must run before a transaction starts.
            dbapi_connection.isolation_level = None
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
                if file_database:
                    cursor.execute("PRAGMA journal_mode=WAL")
                    if cursor.fetchone()[0].lower() != "wal":
                        raise RuntimeError("SQLite database could not enter WAL mode")
            finally:
                cursor.close()

        @event.listens_for(engine, "begin")
        def begin_sqlite(connection):
            connection.exec_driver_sql("BEGIN")

    return engine


class Database:
    """One synchronous transaction boundary per running application.

    Storage callers own the session only inside the context. Never nest
    transactions on this object, await, or share a session between threads.
    """

    def __init__(self, database_url: str | URL):
        self.engine = create_database_engine(database_url)
        self._sessions = sessionmaker(self.engine)
        self._lock = Lock()

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        with self._lock, self._sessions.begin() as session:
            yield session

    def close(self) -> None:
        with self._lock:
            self.engine.dispose()
