"""Composition root for explicit operator commands."""

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from job_market import __version__
from job_market.config import ConfigurationError, load_settings
from job_market.logging_config import configure_logging


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
    return parser


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
