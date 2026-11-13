"""Coordinate bounded Telegram reads, durable checkpoints, and live observations."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from job_market.domain.contracts import MessageObservation
from job_market.ingestion.telegram import Channel, TelegramError, TelegramGateway
from job_market.services.processing import process_pending
from job_market.storage.database import Database
from job_market.storage.messages import MessageStore, SourceState

_LOG = logging.getLogger(__name__)
_PAGE_SIZE = 100
_RECENT_MESSAGES = 100
_SYNC_SECONDS = 60
_PROCESS_SECONDS = 2
_PAGE_PAUSE = 1


class Collector:
    def __init__(
        self,
        gateway: TelegramGateway,
        database: Database,
        store: MessageStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        page_size: int = _PAGE_SIZE,
        page_pause: float = _PAGE_PAUSE,
        sync_seconds: float = _SYNC_SECONDS,
    ):
        self.gateway = gateway
        self.database = database
        self.store = store
        self.clock = clock
        self.page_size = page_size
        self.page_pause = page_pause
        self.sync_seconds = sync_seconds

    async def _backfill(self, channel: Channel, state: SourceState) -> SourceState:
        if state.history_upper_message_id is None:
            head = (
                None if state.history_completed_at is not None else await self.gateway.head(channel)
            )
            state = self.store.pin_history_head(state.id, head)
        while state.history_completed_at is None:
            cursor = state.history_cursor_message_id
            upper = state.history_upper_message_id
            if upper == 0 and cursor is None:
                observations = ()
                complete = True
                last_id = None
            else:
                page = await self.gateway.page(
                    channel,
                    limit=self.page_size,
                    before=cursor or (upper + 1),
                )
                observations = tuple(
                    item
                    for item in page.observations
                    if item.published_at >= state.history_start_at
                )
                complete = not page.ids or len(observations) < len(page.ids) or page.ids[-1] == 1
                last_id = page.ids[-1] if page.ids else None
            state = self.store.store_history_page(
                state.id,
                observations,
                expected_cursor=cursor,
                next_cursor=last_id,
                synced_at=self.clock(),
                completed=complete,
            )
            _LOG.info(
                "telegram_history_page_saved",
                extra={
                    "source_id": state.id,
                    "count": len(observations),
                },
            )
            if not complete:
                await asyncio.sleep(self.page_pause)
        if state.live_checkpoint_message_id is None and state.history_upper_message_id:
            state = self.store.store_live_range(
                state.id,
                (),
                expected_checkpoint=None,
                through_message_id=state.history_upper_message_id,
                synced_at=self.clock(),
            )
        return state

    async def _reconcile(self, channel: Channel, state: SourceState) -> SourceState:
        head = await self.gateway.head(channel)
        cursor = state.live_checkpoint_message_id
        if head is not None and (cursor is None or cursor < head):
            while cursor is None or cursor < head:
                page = await self.gateway.page(
                    channel,
                    limit=self.page_size,
                    after=cursor or 0,
                    before=head + 1,
                    ascending=True,
                )
                # Empty page means the bounded scan reached the end. A short
                # nonempty page isn't proof that there are no more visible rows.
                through = page.ids[-1] if page.ids else head
                state = self.store.store_live_range(
                    state.id,
                    tuple(
                        item
                        for item in page.observations
                        if item.published_at >= state.history_start_at
                    ),
                    expected_checkpoint=cursor,
                    through_message_id=through,
                    synced_at=self.clock(),
                )
                cursor = state.live_checkpoint_message_id
                if cursor < head:
                    await asyncio.sleep(self.page_pause)
        if head is not None:
            recent = await self.gateway.page(channel, limit=_RECENT_MESSAGES, before=head + 1)
            state = self.store.store_live_range(
                state.id,
                tuple(
                    item
                    for item in recent.observations
                    if item.published_at >= state.history_start_at
                ),
                expected_checkpoint=cursor,
                through_message_id=cursor,
                synced_at=self.clock(),
            )
        else:
            state = self.store.store_live_range(
                state.id,
                (),
                expected_checkpoint=cursor,
                through_message_id=cursor,
                synced_at=self.clock(),
            )
        _LOG.info("telegram_reconciled", extra={"source_id": state.id})
        return state

    async def _sync(self, channel: Channel, state: SourceState) -> None:
        state = await self._backfill(channel, state)
        while True:
            state = await self._reconcile(channel, state)
            await asyncio.sleep(self.sync_seconds)

    async def _process(self) -> None:
        recover = True
        while True:
            task = asyncio.create_task(
                asyncio.to_thread(
                    process_pending,
                    self.database,
                    recover=recover,
                    max_requests=10,
                )
            )
            try:
                summary = await asyncio.shield(task)
            except asyncio.CancelledError:
                # A thread cannot be cancelled. Wait for this bounded batch
                # before closing storage or releasing process ownership.
                await task
                raise
            recover = False
            if summary.completed:
                _LOG.info("extractions_processed", extra={"count": summary.completed})
            if summary.failed or summary.recovery_failed:
                _LOG.warning(
                    "operation_failed",
                    extra={
                        "error_category": "parsing",
                        "count": summary.failed + summary.recovery_failed,
                    },
                )
            await asyncio.sleep(_PROCESS_SECONDS)

    async def run(self, channel: Channel, source: SourceState, stop: asyncio.Event) -> None:
        """Run until a stop request, transport disconnect, or storage failure."""
        callback_failed = asyncio.Event()

        def save(observation: MessageObservation) -> None:
            if (
                not stop.is_set()
                and not callback_failed.is_set()
                and observation.published_at >= source.history_start_at
            ):
                self.store.record_observation(source.id, observation)

        with self.gateway.listen(channel, save, callback_failed.set):
            if stop.is_set():
                return
            tasks = {
                asyncio.create_task(self._start_sync(channel, source)),
                asyncio.create_task(self._process()),
                asyncio.create_task(stop.wait()),
                asyncio.create_task(callback_failed.wait()),
                asyncio.create_task(self.gateway.wait_disconnected()),
            }
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
                if callback_failed.is_set():
                    raise TelegramError("A live update could not be stored. Restart to reconcile.")
                if not stop.is_set():
                    raise TelegramError("Telegram disconnected; restart collection to reconcile.")
            finally:
                stop.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _start_sync(self, channel: Channel, source: SourceState) -> None:
        await self.gateway.enable_updates()
        await self._sync(channel, source)
