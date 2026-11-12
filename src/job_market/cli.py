"""Composition root for explicit operator commands."""

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from job_market import __version__
from job_market.config import ConfigurationError, load_settings
from job_market.domain.validation import INT64_MAX
from job_market.logging_config import configure_logging
from job_market.services.processing import installed_versions, process_pending
from job_market.storage.database import Database
from job_market.storage.extractions import ExtractionQueue, StaleClaim, UnknownRevision
from job_market.storage.migration import MigrationError, upgrade_database
from job_market.storage.workspace import WorkspaceError, lock_workspace


def _revision_id(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if not 1 <= number <= INT64_MAX:
        raise argparse.ArgumentTypeError("expected a positive stored revision ID")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="job-market",
        description="Local Telegram job analytics. Only implemented commands are listed.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="project directory for .env and relative runtime paths (default: current directory)",
    )
    commands = parser.add_subparsers(dest="command")
    config = commands.add_parser(
        "config", help="validate local configuration without network access"
    )
    config_commands = config.add_subparsers(dest="config_command", required=True)
    check = config_commands.add_parser(
        "check", help="check settings without displaying their values"
    )
    check.add_argument(
        "--telegram",
        action="store_true",
        help="also validate configured credentials and channel reference locally; never connect",
    )
    db = commands.add_parser("db", help="manage the local database schema")
    db_commands = db.add_subparsers(dest="db_command", required=True)
    db_commands.add_parser("upgrade", help="apply pending Alembic migrations")
    commands.add_parser("process-pending", help="process eligible stored requests offline")
    reprocess = commands.add_parser("reprocess", help="queue new requests for stored revisions")
    reprocess.add_argument("revision_ids", metavar="REVISION_ID", type=_revision_id, nargs="+")
    return parser


def _run_local_work(args: argparse.Namespace, database_url: str) -> int:
    with lock_workspace(database_url):
        database = Database(database_url)
        try:
            if args.command == "reprocess":
                queued = ExtractionQueue(database).enqueue_reprocess(
                    args.revision_ids,
                    installed_versions(),
                    now=datetime.now(UTC),
                )
                print("Queued extraction requests: " + ", ".join(map(str, queued)))
                return 0
            summary = process_pending(database, recover=True)
        finally:
            database.close()
    print(
        f"Completed: {summary.completed}; selected: {summary.selected}; "
        f"failed: {summary.failed}; delayed: {summary.delayed}; "
        f"recovered: {summary.recovered}; exhausted on recovery: {summary.recovery_failed}."
    )
    for reason, count in summary.failure_reasons:
        print(f"{reason.value}: {count}")
    return int(bool(summary.failed or summary.recovery_failed))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0

    # Protect even third-party settings-loader logs before opening .env.
    configure_logging()
    try:
        settings = load_settings(args.project_root)
        configure_logging(settings.log_level)
        if args.command == "db":
            upgrade_database(settings.database_url.get_secret_value())
            print("Database schema is up to date.")
            return 0
        if args.command in {"process-pending", "reprocess"}:
            return _run_local_work(args, settings.database_url.get_secret_value())
        if args.telegram:
            settings.telegram_credentials()
            settings.require_channel()
        logging.getLogger(__name__).info("configuration_validated")
        scope = "including Telegram configuration" if args.telegram else "offline settings only"
        print(f"Configuration is valid ({scope}). No network connection was made.")
        return 0
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except MigrationError as exc:
        print(f"Database migration error: {exc}", file=sys.stderr)
        return 1
    except (WorkspaceError, UnknownRevision, StaleClaim) as exc:
        print(f"Local processing error: {exc}", file=sys.stderr)
        return 1
    except (SQLAlchemyError, RuntimeError):
        print(
            "Local database operation failed. Check workspace access and run 'db upgrade'.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print(
            "Interrupted. An unfinished claim will be recovered on the next run.", file=sys.stderr
        )
        return 130
