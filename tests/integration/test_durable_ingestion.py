"""Offline integration tests for the ingestion and durable queue boundaries."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from job_market.domain.contracts import MessageObservation
from job_market.storage.database import Database
from job_market.storage.extractions import ExtractionQueue, ExtractionVersions
from job_market.storage.messages import MessageStore, SourceUnavailable, StaleCheckpoint
from job_market.storage.migration import upgrade_database

NOW = datetime(2026, 10, 18, 10, tzinfo=UTC)
PUBLISHED = NOW - timedelta(hours=1)
CHAT = -1001234567890
VERSIONS = ExtractionVersions("parser-1", "taxonomy-1")
ORIGINAL = "📈 Vacancy — café\r\n"


@pytest.fixture
def workspace(tmp_path):
    url = f"sqlite:///{tmp_path / 'jobs.db'}"
    upgrade_database(url)
    database = Database(url)
    try:
        yield database, url
    finally:
        database.close()


def register(store, **overrides):
    fields = dict(telegram_chat_id=CHAT, name="Synthetic channel", now=NOW, backfill_days=2)
    return store.register_source(**(fields | overrides))


def observation(number=10, text=ORIGINAL, **overrides):
    fields = dict(
        telegram_chat_id=CHAT,
        telegram_message_id=number,
        published_at=PUBLISHED,
        observed_at=NOW,
        full_text=text,
        content_kind="text",
    )
    return MessageObservation(**(fields | overrides))


def test_replay_reuses_revision_and_pins_one_durable_request(workspace):
    database, _ = workspace
    store = MessageStore(database, VERSIONS)
    source = register(store)
    saved = store.record_observation(source.id, observation())
    updated = MessageStore(database, ExtractionVersions("parser-2", "taxonomy-2"))
    replay = updated.record_observation(
        source.id, observation(observed_at=NOW + timedelta(hours=1))
    )

    assert saved.created_revision
    assert saved.current_revision_id == saved.revision_id
    assert not replay.created_revision
    assert (replay.message_id, replay.revision_id, replay.request_id) == (
        saved.message_id,
        saved.revision_id,
        saved.request_id,
    )
    queue = ExtractionQueue(database)
    claim = queue.claim_next(now=NOW)
    assert claim.id == saved.request_id
    assert claim.versions == VERSIONS
    assert claim.full_text == ORIGINAL
    assert queue.claim_next(now=NOW) is None


def test_history_checkpoint_and_window_survive_restart(workspace):
    database, url = workspace
    store = MessageStore(database, VERSIONS)
    source = register(store)
    page = store.store_history_page(
        source.id,
        [observation(20), observation(16)],
        expected_cursor=None,
        next_cursor=16,
        synced_at=NOW,
    )
    assert page.history_cursor_message_id == 16
    assert page.live_checkpoint_message_id is None

    database.close()
    restarted = Database(url)
    try:
        store = MessageStore(restarted, VERSIONS)
        resumed = register(store, now=NOW + timedelta(days=1), backfill_days=30, name="Renamed")
        assert resumed.id == source.id
        assert resumed.name == "Renamed"
        assert resumed.history_start_at == NOW - timedelta(days=2)
        assert resumed.history_cursor_message_id == 16
        completed = store.store_history_page(
            source.id,
            [observation(12)],
            expected_cursor=16,
            next_cursor=12,
            completed=True,
            synced_at=NOW + timedelta(days=1),
        )
        assert completed.history_cursor_message_id == 12
        assert completed.history_completed_at == NOW + timedelta(days=1)
        assert completed.live_checkpoint_message_id is None
    finally:
        restarted.close()


def test_live_callback_cannot_advance_checkpoint_and_stale_batch_rolls_back(workspace):
    database, _ = workspace
    store = MessageStore(database, VERSIONS)
    source = register(store)
    late = store.record_observation(source.id, observation(100))
    assert store.get_source(source.id).live_checkpoint_message_id is None
    checkpoint = store.store_live_range(
        source.id,
        [observation(10)],
        expected_checkpoint=None,
        through_message_id=20,
        synced_at=NOW,
    )
    assert checkpoint.live_checkpoint_message_id == 20
    with pytest.raises(StaleCheckpoint):
        store.store_live_range(
            source.id,
            [observation(25)],
            expected_checkpoint=None,
            through_message_id=30,
            synced_at=NOW,
        )

    assert store.get_source(source.id).live_checkpoint_message_id == 20
    replay = store.record_observation(source.id, observation(100))
    assert replay.revision_id == late.revision_id
    assert store.record_observation(source.id, observation(25)).created_revision


def test_failed_history_batch_rolls_back_both_messages_and_requests(workspace):
    database, _ = workspace
    store = MessageStore(database, VERSIONS)
    source = register(store)
    with pytest.raises(SourceUnavailable):
        store.store_history_page(
            source.id,
            [observation(20), observation(16, telegram_chat_id=CHAT - 1)],
            expected_cursor=None,
            next_cursor=16,
            synced_at=NOW,
        )

    assert store.get_source(source.id).history_cursor_message_id is None
    assert ExtractionQueue(database).claim_next(now=NOW) is None
    assert store.record_observation(source.id, observation(20)).created_revision


def test_interrupted_claim_survives_restart_without_resetting_budget(workspace):
    database, url = workspace
    store = MessageStore(database, VERSIONS)
    source = register(store)
    saved = store.record_observation(source.id, observation())
    claim = ExtractionQueue(database, max_attempts=2).claim_next(now=NOW)
    assert claim.attempt_count == 1

    database.close()
    restarted = Database(url)
    try:
        queue = ExtractionQueue(restarted, max_attempts=2)
        assert queue.claim_next(now=NOW) is None
        assert queue.get_request(saved.request_id).status == "processing"
        recovered = queue.recover_interrupted(now=NOW + timedelta(minutes=1))
        assert (recovered.requeued, recovered.failed) == (1, 0)
        pending = queue.get_request(saved.request_id)
        assert pending.status == "pending"
        assert pending.attempt_count == 1
        assert pending.versions == VERSIONS

        retry = queue.claim_next(now=NOW + timedelta(minutes=2))
        assert retry.request_key == claim.request_key
        assert retry.attempt_count == 2
        exhausted = queue.recover_interrupted(now=NOW + timedelta(minutes=3))
        assert (exhausted.requeued, exhausted.failed) == (0, 1)
        failed = queue.get_request(saved.request_id)
        assert failed.status == "failed"
        assert failed.attempt_count == 2
        assert failed.finished_at == NOW + timedelta(minutes=3)
        assert failed.last_error == "interrupted_attempt_limit"
        assert queue.claim_next(now=NOW + timedelta(minutes=4)) is None
        again = queue.recover_interrupted(now=NOW + timedelta(minutes=4))
        assert (again.requeued, again.failed) == (0, 0)
    finally:
        restarted.close()


def test_newer_edit_can_revert_text_and_same_text_can_have_a_new_source_version(workspace):
    database, _ = workspace
    store = MessageStore(database, VERSIONS)
    source = register(store)
    first = store.record_observation(source.id, observation())
    edited = store.record_observation(
        source.id,
        observation(text="B", telegram_edited_at=PUBLISHED + timedelta(minutes=5)),
    )
    reverted = store.record_observation(
        source.id,
        observation(telegram_edited_at=PUBLISHED + timedelta(minutes=10)),
    )
    identical_new_edit = observation(telegram_edited_at=PUBLISHED + timedelta(minutes=15))
    stamped = store.record_observation(source.id, identical_new_edit)
    replay = store.record_observation(source.id, identical_new_edit)

    for saved in (first, edited, reverted, stamped):
        assert saved.created_revision
        assert saved.revision_id == saved.current_revision_id
    assert len({saved.revision_id for saved in (first, edited, reverted, stamped)}) == 4
    assert replay.revision_id == stamped.revision_id
    assert not replay.created_revision
    assert (
        store.record_observation(source.id, observation()).current_revision_id
        == stamped.revision_id
    )
    queue = ExtractionQueue(database)
    assert [queue.claim_next(now=NOW).full_text for _ in range(4)] == [
        ORIGINAL,
        "B",
        ORIGINAL,
        ORIGINAL,
    ]
    assert queue.claim_next(now=NOW) is None


@pytest.mark.parametrize("offset", [None, 1, 10])
def test_unordered_observation_is_preserved_without_replacing_current(workspace, offset):
    database, _ = workspace
    store = MessageStore(database, VERSIONS)
    source = register(store)
    current = store.record_observation(
        source.id,
        observation(telegram_edited_at=PUBLISHED + timedelta(minutes=10)),
    )
    unordered = observation(
        text="Unselected version",
        telegram_edited_at=None if offset is None else PUBLISHED + timedelta(minutes=offset),
        observed_at=NOW + timedelta(hours=1),
    )
    saved = store.record_observation(source.id, unordered)
    assert saved.created_revision
    assert saved.revision_id != current.revision_id
    assert saved.current_revision_id == current.revision_id
    replay = store.record_observation(source.id, unordered)
    assert replay.request_id == saved.request_id
    assert not replay.created_revision

    queue = ExtractionQueue(database)
    assert queue.claim_next(now=NOW).revision_id == current.revision_id
    historical = queue.claim_next(now=NOW)
    assert historical.revision_id == saved.revision_id
    assert historical.full_text == "Unselected version"
    assert queue.claim_next(now=NOW) is None


def test_missing_edit_time_cannot_replace_the_initial_unstamped_selection(workspace):
    database, _ = workspace
    store = MessageStore(database, VERSIONS)
    source = register(store)
    current = store.record_observation(source.id, observation())
    conflict = store.record_observation(source.id, observation(text="Another undated version"))
    assert conflict.revision_id != current.revision_id
    assert conflict.current_revision_id == current.revision_id


def test_live_overlap_saves_a_new_edit_below_the_checkpoint(workspace):
    database, _ = workspace
    store = MessageStore(database, VERSIONS)
    source = register(store)
    initial = store.record_observation(source.id, observation(10))
    store.store_live_range(
        source.id,
        [],
        expected_checkpoint=None,
        through_message_id=50,
        synced_at=NOW,
    )
    new_edit = observation(
        10, text="New wording", telegram_edited_at=PUBLISHED + timedelta(minutes=5)
    )
    overlap = store.store_live_range(
        source.id,
        [new_edit],
        expected_checkpoint=50,
        through_message_id=50,
        synced_at=NOW + timedelta(minutes=5),
    )
    replay = store.record_observation(source.id, new_edit)
    assert not replay.created_revision
    assert replay.revision_id != initial.revision_id
    assert replay.current_revision_id == replay.revision_id
    assert overlap.live_checkpoint_message_id == 50
    assert overlap.last_successful_sync_at == NOW + timedelta(minutes=5)
    assert overlap.history_cursor_message_id is None


def test_a_stale_history_page_cannot_reopen_completed_backfill(workspace):
    database, _ = workspace
    store = MessageStore(database, VERSIONS)
    source = register(store)
    store.store_history_page(
        source.id,
        [],
        expected_cursor=None,
        next_cursor=None,
        completed=True,
        synced_at=NOW,
    )
    with pytest.raises(StaleCheckpoint):
        store.store_history_page(
            source.id,
            [observation(50)],
            expected_cursor=None,
            next_cursor=50,
            synced_at=NOW,
        )
    assert store.get_source(source.id).history_completed_at == NOW
    assert ExtractionQueue(database).claim_next(now=NOW) is None


def test_request_insert_failure_rolls_back_new_revision_and_checkpoint(workspace):
    database, _ = workspace
    store = MessageStore(database, VERSIONS)
    source = register(store)
    # Simulate a storage failure after the revision insert, before the queue
    # insert can complete. Use a real SQLite failure, not a mocked repository.
    with database.engine.begin() as connection:
        connection.exec_driver_sql("""
            CREATE TRIGGER reject_test_request BEFORE INSERT ON extraction_runs
            BEGIN SELECT RAISE(ABORT, 'synthetic request write failure'); END
        """)
    with pytest.raises(IntegrityError, match="synthetic request write failure"):
        store.store_live_range(
            source.id,
            [observation()],
            expected_checkpoint=None,
            through_message_id=10,
            synced_at=NOW,
        )
    assert store.get_source(source.id).live_checkpoint_message_id is None
    assert ExtractionQueue(database).claim_next(now=NOW) is None
    with database.engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER reject_test_request")
    saved = store.record_observation(source.id, observation())
    assert saved.created_revision
    assert ExtractionQueue(database).claim_next(now=NOW).id == saved.request_id


def test_second_channel_cannot_be_enabled_implicitly(workspace):
    database, _ = workspace
    store = MessageStore(database, VERSIONS)
    source = register(store)
    with pytest.raises(SourceUnavailable, match="different channel"):
        register(store, telegram_chat_id=CHAT + 1)
    assert store.get_source(source.id).telegram_chat_id == CHAT


def test_duplicate_callbacks_in_one_process_share_a_revision_and_request(workspace):
    database, _ = workspace
    store = MessageStore(database, VERSIONS)
    source = register(store)
    item = observation(text="", content_kind="unsupported")
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda _: store.record_observation(source.id, item), range(8)))
    assert sum(saved.created_revision for saved in results) == 1
    assert len({saved.revision_id for saved in results}) == 1
    assert len({saved.request_id for saved in results}) == 1
    queue = ExtractionQueue(database)
    claim = queue.claim_next(now=NOW)
    assert claim.content_kind == "unsupported"
    assert claim.full_text == ""
    assert queue.claim_next(now=NOW) is None
