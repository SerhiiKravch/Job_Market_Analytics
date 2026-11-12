"""Persist channel identity, original observations, revisions, and checkpoints."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256

from sqlalchemy import case, func, literal, select, update
from sqlalchemy.orm import Session

from job_market.domain.contracts import MessageObservation
from job_market.domain.validation import INT64_MAX, INT64_MIN, as_utc, nonblank
from job_market.storage.database import Database
from job_market.storage.extractions import ExtractionVersions, ensure_automatic_request
from job_market.storage.models import Message, MessageRevision, Source


class SourceUnavailable(ValueError):
    """The requested Telegram channel is not the enabled source."""


class StaleCheckpoint(ValueError):
    """Reconciliation must restart from the checkpoint in storage."""


@dataclass(frozen=True)
class SourceState:
    id: int
    telegram_chat_id: int
    name: str
    username: str | None
    history_start_at: datetime
    history_cursor_message_id: int | None
    history_completed_at: datetime | None
    live_checkpoint_message_id: int | None
    last_successful_sync_at: datetime | None


@dataclass(frozen=True)
class StoredObservation:
    message_id: int
    revision_id: int
    current_revision_id: int
    request_id: int
    created_revision: bool


def _source_state(source: Source) -> SourceState:
    return SourceState(
        id=source.id,
        telegram_chat_id=source.telegram_chat_id,
        name=source.name,
        username=source.username,
        history_start_at=source.history_start_at,
        history_cursor_message_id=source.history_cursor_message_id,
        history_completed_at=source.history_completed_at,
        live_checkpoint_message_id=source.live_checkpoint_message_id,
        last_successful_sync_at=source.last_successful_sync_at,
    )


def _require_source(session: Session, source_id: int) -> Source:
    source = session.get(Source, source_id)
    if source is None or not source.enabled:
        raise SourceUnavailable("The selected channel is not an enabled source.")
    return source


def _validate_cursor(cursor: int | None) -> None:
    if cursor is not None and (type(cursor) is not int or not 1 <= cursor <= INT64_MAX):
        raise ValueError("A checkpoint must be a positive Telegram message ID or null.")


def _latest_sync(synced_at: datetime):
    return case(
        (
            Source.last_successful_sync_at.is_(None) | (Source.last_successful_sync_at < synced_at),
            literal(synced_at, type_=Source.last_successful_sync_at.type),
        ),
        else_=Source.last_successful_sync_at,
    )


class MessageStore:
    def __init__(self, database: Database, versions: ExtractionVersions):
        self._database = database
        self._versions = versions

    def register_source(
        self,
        *,
        telegram_chat_id: int,
        name: str,
        now: datetime,
        backfill_days: int,
        username: str | None = None,
    ) -> SourceState:
        """Create the one enabled source or refresh its display metadata on restart."""
        if type(telegram_chat_id) is not int or not INT64_MIN <= telegram_chat_id <= INT64_MAX:
            raise ValueError("A signed 64-bit Telegram chat ID is required.")
        if type(backfill_days) is not int or not 1 <= backfill_days <= 30:
            raise ValueError("Backfill days must be between 1 and 30.")
        nonblank(name)
        now = as_utc(now)
        with self._database.transaction() as session:
            source = session.scalar(
                select(Source).where(Source.telegram_chat_id == telegram_chat_id)
            )
            if source is None:
                if session.scalar(select(Source.id).where(Source.enabled).limit(1)) is not None:
                    raise SourceUnavailable("A different channel is already enabled.")
                source = Source(
                    telegram_chat_id=telegram_chat_id,
                    source_type="channel",
                    name=name,
                    username=username,
                    history_start_at=now - timedelta(days=backfill_days),
                    created_at=now,
                )
                session.add(source)
                session.flush()
            else:
                if not source.enabled:
                    raise SourceUnavailable("The configured channel is disabled.")
                source.name = name
                source.username = username
            return _source_state(source)

    def get_source(self, source_id: int) -> SourceState:
        with self._database.transaction() as session:
            return _source_state(_require_source(session, source_id))

    def store_history_page(
        self,
        source_id: int,
        observations: Sequence[MessageObservation],
        *,
        expected_cursor: int | None,
        next_cursor: int | None,
        synced_at: datetime,
        completed: bool = False,
    ) -> SourceState:
        """Save a descending page already fetched from the source, and its cursor."""
        _validate_cursor(expected_cursor)
        _validate_cursor(next_cursor)
        if next_cursor is None and not completed:
            raise ValueError("An incomplete history page must advance the cursor.")
        if (
            next_cursor is not None
            and expected_cursor is not None
            and next_cursor >= expected_cursor
        ):
            raise ValueError("The history cursor must move toward older IDs.")
        synced_at = as_utc(synced_at)
        with self._database.transaction() as session:
            source = _require_source(session, source_id)
            for item in observations:
                message_id = item.telegram_message_id
                if (
                    (expected_cursor is not None and message_id >= expected_cursor)
                    or (next_cursor is not None and message_id < next_cursor)
                    or item.published_at < source.history_start_at
                ):
                    raise ValueError(
                        "A history observation lies outside the requested page or window."
                    )
                self._record(session, source, item)
            changed = session.execute(
                update(Source)
                .where(
                    Source.id == source_id,
                    Source.enabled,
                    Source.history_completed_at.is_(None),
                    Source.history_cursor_message_id == expected_cursor,
                )
                .values(
                    history_cursor_message_id=next_cursor
                    if next_cursor is not None
                    else expected_cursor,
                    history_completed_at=synced_at if completed else None,
                    last_successful_sync_at=_latest_sync(synced_at),
                )
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                raise StaleCheckpoint("History cursor changed or history is already complete.")
            session.refresh(source)
            return _source_state(source)

    def store_live_range(
        self,
        source_id: int,
        observations: Sequence[MessageObservation],
        *,
        expected_checkpoint: int | None,
        through_message_id: int | None,
        synced_at: datetime,
    ) -> SourceState:
        """Commit a reconciled range, optionally including older observations for edits."""
        _validate_cursor(expected_checkpoint)
        _validate_cursor(through_message_id)
        if expected_checkpoint is not None and (
            through_message_id is None or through_message_id < expected_checkpoint
        ):
            raise ValueError("The live checkpoint cannot move backward.")
        synced_at = as_utc(synced_at)
        with self._database.transaction() as session:
            source = _require_source(session, source_id)
            for item in observations:
                if through_message_id is None or item.telegram_message_id > through_message_id:
                    raise ValueError("A live observation exceeds the reconciled range.")
                self._record(session, source, item)
            changed = session.execute(
                update(Source)
                .where(
                    Source.id == source_id,
                    Source.enabled,
                    Source.live_checkpoint_message_id == expected_checkpoint,
                )
                .values(
                    live_checkpoint_message_id=through_message_id,
                    last_successful_sync_at=_latest_sync(synced_at),
                )
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                raise StaleCheckpoint("Live checkpoint changed; reread it before retrying.")
            session.refresh(source)
            return _source_state(source)

    def record_observation(
        self,
        source_id: int,
        observation: MessageObservation,
    ) -> StoredObservation:
        """Persist a callback without treating it as proof of reconciled coverage."""
        with self._database.transaction() as session:
            source = _require_source(session, source_id)
            return self._record(session, source, observation)

    def _record(
        self,
        session: Session,
        source: Source,
        observation: MessageObservation,
    ) -> StoredObservation:
        if observation.telegram_chat_id != source.telegram_chat_id:
            raise SourceUnavailable("The observation belongs to a different channel.")
        message = session.scalar(
            select(Message).where(
                Message.source_id == source.id,
                Message.telegram_message_id == observation.telegram_message_id,
            )
        )
        if message is None:
            message = Message(
                source_id=source.id,
                telegram_message_id=observation.telegram_message_id,
                published_at=observation.published_at,
                first_seen_at=observation.observed_at,
                last_seen_at=observation.observed_at,
                message_url=observation.message_url,
            )
            session.add(message)
            session.flush()
        else:
            message.first_seen_at = min(message.first_seen_at, observation.observed_at)
            message.last_seen_at = max(message.last_seen_at, observation.observed_at)
            if message.message_url is None:
                message.message_url = observation.message_url

        digest = sha256(observation.full_text.encode("utf-8")).hexdigest()
        matching = session.scalars(
            select(MessageRevision)
            .where(
                MessageRevision.message_id == message.id,
                MessageRevision.content_hash == digest,
                MessageRevision.content_kind == observation.content_kind,
                MessageRevision.telegram_edited_at == observation.telegram_edited_at,
            )
            .order_by(MessageRevision.revision_number)
        )
        revision = next((row for row in matching if row.full_text == observation.full_text), None)
        created = revision is None
        if revision is None:
            latest = session.scalar(
                select(func.max(MessageRevision.revision_number)).where(
                    MessageRevision.message_id == message.id
                )
            )
            revision = MessageRevision(
                message_id=message.id,
                revision_number=(latest or 0) + 1,
                full_text=observation.full_text,
                content_kind=observation.content_kind,
                telegram_edited_at=observation.telegram_edited_at,
                observed_at=observation.observed_at,
                content_hash=digest,
            )
            session.add(revision)
            session.flush()
            current = (
                session.get(MessageRevision, message.current_revision_id)
                if (message.current_revision_id is not None)
                else None
            )
            edit_at = observation.telegram_edited_at
            # An unstamped initial Telegram version uses publication as its
            # baseline; an unstamped incoming variant never replaces a selection.
            if current is None or (
                edit_at is not None
                and edit_at > (current.telegram_edited_at or message.published_at)
            ):
                message.current_revision_id = revision.id

        request_id = ensure_automatic_request(
            session,
            revision.id,
            self._versions,
            observed_at=revision.observed_at,
        )
        if message.current_revision_id is None:
            raise RuntimeError("The stored message has no current revision.")
        return StoredObservation(
            message.id,
            revision.id,
            message.current_revision_id,
            request_id,
            created,
        )
