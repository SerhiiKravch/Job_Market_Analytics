"""Durable automatic request identity and the extraction claim boundary."""

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from job_market.domain.enums import ContentKind, ExtractionOrigin, ExtractionStatus
from job_market.domain.validation import as_utc, nonblank
from job_market.storage.database import Database
from job_market.storage.models import ExtractionRun, MessageRevision


@dataclass(frozen=True)
class ExtractionVersions:
    parser: str
    taxonomy: str

    def __post_init__(self) -> None:
        nonblank(self.parser)
        nonblank(self.taxonomy)


@dataclass(frozen=True)
class ClaimedExtraction:
    id: int
    revision_id: int
    request_key: str
    versions: ExtractionVersions
    attempt_count: int
    started_at: datetime
    content_kind: ContentKind
    full_text: str = field(repr=False)


@dataclass(frozen=True)
class ExtractionRequestState:
    id: int
    revision_id: int
    request_key: str
    versions: ExtractionVersions
    status: ExtractionStatus
    attempt_count: int
    started_at: datetime | None
    finished_at: datetime | None
    last_error: str | None = field(repr=False)


@dataclass(frozen=True)
class RecoveryResult:
    requeued: int
    failed: int


def ensure_automatic_request(
    session: Session,
    revision_id: int,
    versions: ExtractionVersions,
    *,
    observed_at: datetime,
) -> int:
    """Reuse the initial request for this revision inside the caller's transaction."""
    key = f"automatic:revision:{revision_id}"
    run = session.scalar(select(ExtractionRun).where(ExtractionRun.request_key == key))
    if run is None:
        run = ExtractionRun(
            message_revision_id=revision_id,
            request_key=key,
            origin=ExtractionOrigin.AUTOMATIC,
            status=ExtractionStatus.PENDING,
            parser_version=versions.parser,
            taxonomy_version=versions.taxonomy,
            created_at=observed_at,
        )
        session.add(run)
        session.flush()
    elif run.message_revision_id != revision_id or run.origin != ExtractionOrigin.AUTOMATIC:
        raise RuntimeError("Automatic extraction request key has an invalid owner.")
    return run.id


class ExtractionQueue:
    def __init__(self, database: Database, *, max_attempts: int = 3):
        if type(max_attempts) is not int or not 1 <= max_attempts < 2**31:
            raise ValueError("The attempt limit must be a positive 32-bit integer.")
        self._database = database
        self._max_attempts = max_attempts

    def get_request(self, request_id: int) -> ExtractionRequestState | None:
        with self._database.transaction() as session:
            run = session.get(ExtractionRun, request_id)
            if run is None:
                return None
            return ExtractionRequestState(
                id=run.id,
                revision_id=run.message_revision_id,
                request_key=run.request_key,
                versions=ExtractionVersions(run.parser_version, run.taxonomy_version),
                status=ExtractionStatus(run.status),
                attempt_count=run.attempt_count,
                started_at=run.started_at,
                finished_at=run.finished_at,
                last_error=run.last_error,
            )

    def recover_interrupted(self, *, now: datetime) -> RecoveryResult:
        """Recover at startup only after the caller holds exclusive workspace ownership.

        Processing claims are not leases. Do not call this while any worker can
        still be processing an attempt. The runtime process lock is a later step.
        """
        now = as_utc(now)
        with self._database.transaction() as session:
            failed = self._fail_exhausted(
                session,
                ExtractionStatus.PROCESSING,
                now,
                "interrupted_attempt_limit",
            )
            requeued = session.execute(
                update(ExtractionRun)
                .where(ExtractionRun.status == ExtractionStatus.PROCESSING)
                .values(
                    status=ExtractionStatus.PENDING,
                    finished_at=None,
                    last_error="interrupted",
                )
            ).rowcount
            return RecoveryResult(requeued=requeued, failed=failed)

    def _fail_exhausted(
        self,
        session: Session,
        status: ExtractionStatus,
        now: datetime,
        reason: str,
    ) -> int:
        return session.execute(
            update(ExtractionRun)
            .where(
                ExtractionRun.status == status,
                ExtractionRun.attempt_count >= self._max_attempts,
            )
            .values(status=ExtractionStatus.FAILED, finished_at=now, last_error=reason)
        ).rowcount

    def claim_next(self, *, now: datetime) -> ClaimedExtraction | None:
        now = as_utc(now)
        with self._database.transaction() as session:
            # An operator can lower the retry limit between runs.
            self._fail_exhausted(session, ExtractionStatus.PENDING, now, "attempt_limit_reached")
            row = session.execute(
                select(ExtractionRun, MessageRevision)
                .join(MessageRevision, MessageRevision.id == ExtractionRun.message_revision_id)
                .where(
                    ExtractionRun.status == ExtractionStatus.PENDING,
                    ExtractionRun.attempt_count < self._max_attempts,
                )
                .order_by(ExtractionRun.id)
                .limit(1)
            ).one_or_none()
            if row is None:
                return None
            run, revision = row
            run.status = ExtractionStatus.PROCESSING
            run.attempt_count += 1
            run.started_at = now
            return ClaimedExtraction(
                id=run.id,
                revision_id=revision.id,
                request_key=run.request_key,
                versions=ExtractionVersions(run.parser_version, run.taxonomy_version),
                attempt_count=run.attempt_count,
                started_at=now,
                content_kind=ContentKind(revision.content_kind),
                full_text=revision.full_text,
            )
