"""Composition root for explicit operator commands."""

import argparse
import asyncio
import json
import logging
import signal
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from job_market import __version__
from job_market.config import ConfigurationError, Settings, load_settings
from job_market.domain.validation import INT64_MAX
from job_market.logging_config import configure_logging
from job_market.services.processing import installed_versions, process_pending
from job_market.storage.database import Database
from job_market.storage.extractions import ExtractionQueue, StaleClaim, UnknownRevision
from job_market.storage.messages import MessageStore, SourceUnavailable, StaleCheckpoint
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
    telegram = commands.add_parser("telegram", help="manage the Telegram user session")
    telegram_commands = telegram.add_subparsers(dest="telegram_command", required=True)
    telegram_commands.add_parser("login", help="sign in interactively")
    telegram_commands.add_parser("sources", help="list accessible broadcast channel dialogs")
    commands.add_parser("collect", help="collect and process the configured Telegram channel")
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


async def _telegram_command(args: argparse.Namespace, settings: Settings) -> int:
    from job_market.ingestion.service import Collector
    from job_market.ingestion.telegram import hidden_prompt, open_telegram

    credentials = settings.telegram_credentials()
    if args.command == "telegram":
        login = args.telegram_command == "login"
        async with open_telegram(
            settings.telegram_session_path, credentials, login=login
        ) as gateway:
            if login:
                created = await gateway.login(hidden_prompt)
                print(
                    "Telegram login complete."
                    if created
                    else "Telegram session is already signed in."
                )
            else:
                await gateway.require_user()
                for channel in await gateway.sources():
                    # Explicit operator output, not a log; escape terminal controls.
                    print(
                        json.dumps(
                            {
                                "chat_id": channel.chat_id,
                                "title": channel.title,
                                "username": channel.username,
                            },
                            ensure_ascii=True,
                        )
                    )
        return 0

    reference = settings.require_channel()
    database_url = settings.database_url.get_secret_value()
    loop = asyncio.get_running_loop()
    current = asyncio.current_task()
    with lock_workspace(database_url):
        loop.add_signal_handler(signal.SIGTERM, current.cancel)
        try:
            database = Database(database_url)
            try:
                async with open_telegram(settings.telegram_session_path, credentials) as gateway:
                    await gateway.require_user()
                    channel = await gateway.resolve(reference)
                    store = MessageStore(database, installed_versions())
                    source = store.register_source(
                        telegram_chat_id=channel.info.chat_id,
                        name=channel.info.title,
                        username=channel.info.username,
                        now=datetime.now(UTC),
                        backfill_days=settings.backfill_days,
                    )
                    logging.getLogger(__name__).info(
                        "application_started", extra={"source_id": source.id}
                    )
                    await Collector(gateway, database, store).run(channel, source, asyncio.Event())
            finally:
                database.close()
        finally:
            loop.remove_signal_handler(signal.SIGTERM)
    return 0


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
        if args.command in {"telegram", "collect"}:
            from job_market.ingestion.telegram import TelegramError

            try:
                return asyncio.run(_telegram_command(args, settings))
            except (TelegramError, SourceUnavailable, StaleCheckpoint) as exc:
                print(f"Telegram command failed: {exc}", file=sys.stderr)
                return 1
            except asyncio.CancelledError:
                return 0
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
