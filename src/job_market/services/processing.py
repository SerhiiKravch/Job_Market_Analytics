"""Drain eligible extraction work without connecting to Telegram."""

import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import OperationalError, SQLAlchemyError

from job_market.domain.enums import ExtractionStatus
from job_market.parsing.pipeline import PARSER_VERSION, parse_vacancy
from job_market.parsing.taxonomy import load_taxonomy
from job_market.storage.database import Database
from job_market.storage.extractions import (
    ExtractionQueue,
    ExtractionVersions,
    FailureReason,
    InvalidResult,
    KeywordEntry,
)


@dataclass(frozen=True)
class ProcessingSummary:
    completed: int
    selected: int
    failed: int
    delayed: int
    recovered: int
    recovery_failed: int
    failure_reasons: tuple[tuple[FailureReason, int], ...]


def installed_versions() -> ExtractionVersions:
    return ExtractionVersions(PARSER_VERSION, load_taxonomy().version)


def _now() -> datetime:
    return datetime.now(UTC)


def _write_failure(error: SQLAlchemyError) -> FailureReason:
    if isinstance(error, OperationalError) and isinstance(error.orig, sqlite3.OperationalError):
        code = getattr(error.orig, "sqlite_errorcode", 0)
        if code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            return FailureReason.STORAGE_BUSY
    return FailureReason.STORAGE_ERROR


def process_pending(
    database: Database,
    *,
    clock: Callable[[], datetime] = _now,
    parser=parse_vacancy,
    recover: bool = False,
    max_requests: int | None = None,
) -> ProcessingSummary:
    """Process ready work; caller must hold the workspace lock for recovery."""
    queue = ExtractionQueue(database)
    taxonomy = load_taxonomy()
    versions = ExtractionVersions(PARSER_VERSION, taxonomy.version)
    vocabulary = tuple(
        KeywordEntry(
            entry.canonical_code,
            entry.display_name,
            entry.category_code,
            entry.description,
            entry.active,
        )
        for entry in taxonomy.keywords
    )
    recovery = queue.recover_interrupted(now=clock()) if recover else None
    completed = selected = failed = 0
    reasons: Counter[FailureReason] = Counter()
    handled = 0
    while max_requests is None or handled < max_requests:
        claim = queue.claim_next(now=clock())
        if claim is None:
            break
        handled += 1
        if claim.versions != versions:
            reason = FailureReason.VERSION_UNAVAILABLE
        else:
            try:
                result = parser(claim.full_text, content_kind=claim.content_kind)
            except ValueError:
                reason = FailureReason.INVALID_RESULT
            except Exception:
                reason = FailureReason.PARSER_ERROR
            else:
                try:
                    activated = queue.complete(claim, result, vocabulary, now=clock())
                except InvalidResult:
                    reason = FailureReason.INVALID_RESULT
                except SQLAlchemyError as exc:
                    reason = _write_failure(exc)
                else:
                    completed += 1
                    selected += activated
                    continue
        # If a database outage also prevents this write, the durable processing
        # claim is left intact for the next exclusively owned recovery.
        status = queue.record_failure(claim, reason, now=clock())
        reasons[reason] += 1
        failed += status == ExtractionStatus.FAILED
    return ProcessingSummary(
        completed,
        selected,
        failed,
        queue.delayed_count(now=clock()),
        recovery.requeued if recovery else 0,
        recovery.failed if recovery else 0,
        tuple(sorted(reasons.items())),
    )
