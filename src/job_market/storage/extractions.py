"""Durable automatic request identity and the extraction claim boundary."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from job_market.domain.contracts import ExtractionResult
from job_market.domain.enums import (
    ContentKind,
    ExtractionOrigin,
    ExtractionStatus,
    KeywordCategory,
    ReviewStatus,
)
from job_market.domain.validation import INT64_MAX, as_utc, nonblank
from job_market.storage import models
from job_market.storage.database import Database
from job_market.storage.models import ExtractionRun, Message, MessageRevision


class StaleClaim(ValueError):
    """The requested attempt no longer owns the processing claim."""


class InvalidResult(ValueError):
    """The interpretation cannot be persisted as a validated snapshot."""


class UnknownRevision(ValueError):
    """At least one explicitly requested revision does not exist."""


class FailureReason(StrEnum):
    VERSION_UNAVAILABLE = "version_unavailable"
    INVALID_RESULT = "invalid_result"
    PARSER_ERROR = "parser_error"
    STORAGE_ERROR = "storage_error"
    STORAGE_BUSY = "storage_busy"


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
    retry_not_before: datetime | None


@dataclass(frozen=True)
class RecoveryResult:
    requeued: int
    failed: int


@dataclass(frozen=True)
class KeywordEntry:
    canonical_code: str
    display_name: str
    category_code: KeywordCategory
    description: str | None
    active: bool


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
                retry_not_before=run.retry_not_before,
            )

    def recover_interrupted(self, *, now: datetime) -> RecoveryResult:
        """Recover at startup only after the caller holds exclusive workspace ownership.

        Processing claims are not leases. Do not call this while any worker can
        still be processing an attempt.
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
            .values(
                status=ExtractionStatus.FAILED,
                finished_at=now,
                last_error=reason,
                retry_not_before=None,
            )
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
                    or_(
                        ExtractionRun.retry_not_before.is_(None),
                        ExtractionRun.retry_not_before <= now,
                    ),
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
            run.retry_not_before = None
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

    def delayed_count(self, *, now: datetime) -> int:
        now = as_utc(now)
        with self._database.transaction() as session:
            return session.scalar(
                select(func.count())
                .select_from(ExtractionRun)
                .where(
                    ExtractionRun.status == ExtractionStatus.PENDING,
                    ExtractionRun.retry_not_before > now,
                )
            )

    def enqueue_reprocess(
        self,
        revision_ids: list[int],
        versions: ExtractionVersions,
        *,
        now: datetime,
    ) -> tuple[int, ...]:
        """Queue one distinct request per selected revision, all or nothing."""
        now = as_utc(now)
        if not revision_ids or any(
            type(id_) is not int or not 1 <= id_ <= INT64_MAX for id_ in revision_ids
        ):
            raise UnknownRevision("Provide positive stored revision IDs.")
        with self._database.transaction() as session:
            requests = []
            for revision_id in dict.fromkeys(revision_ids):
                if session.get(MessageRevision, revision_id) is None:
                    raise UnknownRevision(f"Revision {revision_id} does not exist.")
                requests.append(
                    ExtractionRun(
                        message_revision_id=revision_id,
                        request_key=f"reprocess:{uuid4().hex}",
                        origin=ExtractionOrigin.AUTOMATIC,
                        status=ExtractionStatus.PENDING,
                        parser_version=versions.parser,
                        taxonomy_version=versions.taxonomy,
                        created_at=now,
                    )
                )
            session.add_all(requests)
            session.flush()
            return tuple(run.id for run in requests)

    def record_failure(
        self,
        claim: ClaimedExtraction,
        reason: FailureReason,
        *,
        now: datetime,
    ) -> ExtractionStatus:
        """Retry only SQLite busy/locked failures, with a durable bounded delay."""
        now = as_utc(now)
        reason = FailureReason(reason)
        with self._database.transaction() as session:
            run = _owned_run(session, claim)
            run.last_error = reason.value
            if reason == FailureReason.STORAGE_BUSY and run.attempt_count < self._max_attempts:
                delay = timedelta(seconds=5 * 2 ** min(run.attempt_count - 1, 4))
                run.status = ExtractionStatus.PENDING
                run.retry_not_before = now + delay
                run.finished_at = None
                return ExtractionStatus.PENDING
            run.status = ExtractionStatus.FAILED
            run.finished_at = now
            run.retry_not_before = None
            return ExtractionStatus.FAILED

    def complete(
        self,
        claim: ClaimedExtraction,
        result: ExtractionResult,
        vocabulary: tuple[KeywordEntry, ...],
        *,
        now: datetime,
    ) -> bool:
        """Atomically complete a claim, returning whether it became active."""
        now = as_utc(now)
        with self._database.transaction() as session:
            run = _owned_run(session, claim)
            revision = session.get(MessageRevision, run.message_revision_id)
            if not isinstance(result, ExtractionResult):
                raise InvalidResult("The parser did not return an extraction result.")
            if (result.parser_version, result.taxonomy_version) != (
                run.parser_version,
                run.taxonomy_version,
            ):
                raise InvalidResult("Result versions do not match the request.")
            try:
                result = ExtractionResult.model_validate(result.model_dump())
                result.validate_source(revision.full_text)
            except ValueError:
                raise InvalidResult(
                    "The extraction failed snapshot or source validation."
                ) from None
            payload = result.model_dump(mode="json")
            _save_snapshot(session, run, payload, vocabulary, now)
            run.classification = result.classification
            run.classification_evidence = payload["classification_evidence"]
            run.status = ExtractionStatus.COMPLETED
            run.finished_at = now
            run.last_error = None
            run.retry_not_before = None
            run.review_status = (
                ReviewStatus.NEEDS_REVIEW if result.issues else ReviewStatus.NOT_REQUIRED
            )
            # The active FK requires a completed run before the pointer moves.
            session.flush()
            return _select_automatic(session, revision, run.id)


def _owned_run(session: Session, claim: ClaimedExtraction) -> ExtractionRun:
    run = session.get(ExtractionRun, claim.id)
    if run is None or (
        run.status,
        run.origin,
        run.attempt_count,
        run.message_revision_id,
        run.request_key,
        run.started_at,
    ) != (
        ExtractionStatus.PROCESSING,
        ExtractionOrigin.AUTOMATIC,
        claim.attempt_count,
        claim.revision_id,
        claim.request_key,
        claim.started_at,
    ):
        raise StaleClaim("The extraction attempt no longer owns the claim.")
    return run


def _select_automatic(session: Session, revision: MessageRevision, run_id: int) -> bool:
    message = session.get(Message, revision.message_id)
    if message.current_revision_id != revision.id:
        return False
    if revision.active_extraction_run_id is not None:
        active = session.get(ExtractionRun, revision.active_extraction_run_id)
        if active.origin == ExtractionOrigin.MANUAL or active.id >= run_id:
            return False
    revision.active_extraction_run_id = run_id
    return True


def _save_snapshot(
    session: Session,
    run: ExtractionRun,
    payload: dict,
    vocabulary: tuple[KeywordEntry, ...],
    now: datetime,
) -> None:
    posting_data = payload["posting"]
    posting_id = None
    if posting_data is not None:
        experience = posting_data.pop("experience_requirements")
        salaries = posting_data.pop("salary_offers")
        mentions = posting_data.pop("keyword_mentions")
        keywords = _keyword_ids(session, vocabulary, experience, mentions)
        posting = models.JobPosting(extraction_run_id=run.id, **posting_data)
        session.add(posting)
        session.flush()
        posting_id = posting.id
        for item in experience:
            code = item.pop("keyword_code")
            session.add(
                models.ExperienceRequirement(
                    job_posting_id=posting_id,
                    keyword_id=keywords.get(code),
                    **item,
                )
            )
        for item in salaries:
            session.add(models.SalaryOffer(job_posting_id=posting_id, **item))
        for item in mentions:
            code = item.pop("keyword_code")
            session.add(
                models.KeywordMention(
                    job_posting_id=posting_id,
                    keyword_id=keywords[code],
                    **item,
                )
            )
    for issue in payload["issues"]:
        session.add(
            models.ReviewIssue(
                extraction_run_id=run.id,
                message_revision_id=run.message_revision_id,
                job_posting_id=posting_id if issue["field_path"].startswith("posting") else None,
                created_at=now,
                **issue,
            )
        )


def _keyword_ids(
    session: Session,
    vocabulary: tuple[KeywordEntry, ...],
    experience: list[dict],
    mentions: list[dict],
) -> dict[str, int]:
    needed = {item["keyword_code"] for item in (*experience, *mentions)} - {None}
    entries = {entry.canonical_code: entry for entry in vocabulary if entry.active}
    if not needed <= entries.keys():
        raise InvalidResult("A keyword is absent from the pinned taxonomy.")
    if not needed:
        return {}
    existing = {
        row.canonical_code: row
        for row in session.scalars(
            select(models.Keyword).where(models.Keyword.canonical_code.in_(needed))
        )
    }
    for code in sorted(needed):
        entry = entries[code]
        row = existing.get(code)
        if row is None:
            row = models.Keyword(
                canonical_code=code,
                display_name=entry.display_name,
                category_code=entry.category_code,
                description=entry.description,
                active=True,
            )
            session.add(row)
            existing[code] = row
        elif row.category_code != entry.category_code:
            raise InvalidResult("A stable keyword has a conflicting stored category.")
        else:
            row.display_name = entry.display_name
            row.description = entry.description
            row.active = True
    session.flush()
    return {code: row.id for code, row in existing.items()}
