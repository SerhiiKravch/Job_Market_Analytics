"""Process ownership for local SQLite worker commands."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from stat import S_ISREG

from sqlalchemy.engine import make_url


class WorkspaceError(ValueError):
    """A safe operator-facing workspace error."""


@contextmanager
def lock_workspace(database_url: str) -> Iterator[None]:
    """Hold an advisory lock beside the resolved SQLite file until work ends."""
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or url.database in {None, "", ":memory:"}:
        raise WorkspaceError("Local processing requires a file-backed SQLite database.")
    try:
        path = Path(url.database).resolve()
        if not path.is_file():
            raise WorkspaceError("Database does not exist. Run 'db upgrade' first.")
        if path.stat().st_nlink != 1:
            raise WorkspaceError("Use a SQLite database without filesystem hard links.")
    except (OSError, RuntimeError):
        raise WorkspaceError("Could not inspect the database workspace.") from None
    with lock_file(path.with_name(path.name + ".worker.lock"), "database workspace"):
        yield


@contextmanager
def lock_file(lock_path: Path, resource: str) -> Iterator[None]:
    """Use the same nonblocking ownership policy for a database or session."""
    try:
        import fcntl
    except ImportError:
        raise WorkspaceError("Local processing requires POSIX file locking.") from None
    try:
        descriptor = os.open(
            lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
        )
    except (OSError, RuntimeError):
        raise WorkspaceError(f"Could not open the {resource} lock.") from None
    try:
        if not S_ISREG(os.fstat(descriptor).st_mode):
            raise WorkspaceError("The workspace lock must be a regular file.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WorkspaceError(f"Another worker owns this {resource}.") from None
        except OSError:
            raise WorkspaceError(f"Could not acquire the {resource} lock.") from None
        yield
    finally:
        # Closing releases the OS lock. Leave the file so contenders use one inode.
        os.close(descriptor)
