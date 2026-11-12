"""Exercise processing against the migrated SQLite database without Telegram."""

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import select

from job_market.domain.contracts import Evidence, MessageObservation
from job_market.parsing.pipeline import PARSER_VERSION, parse_vacancy
from job_market.parsing.taxonomy import load_taxonomy
from job_market.services.processing import process_pending
from job_market.storage.database import Database
from job_market.storage.extractions import (
    ExtractionQueue,
    ExtractionVersions,
    FailureReason,
    KeywordEntry,
    StaleClaim,
)
from job_market.storage.messages import MessageStore
from job_market.storage.migration import migration_config, upgrade_database
from job_market.storage.models import (
    ExtractionRun,
    JobPosting,
    Keyword,
    KeywordMention,
    MessageRevision,
    ReviewIssue,
    SalaryOffer,
)
from job_market.storage.workspace import lock_workspace

NOW = datetime(2026, 11, 13, tzinfo=UTC)
CHAT = -1001234567890
VERSIONS = ExtractionVersions(PARSER_VERSION, load_taxonomy().version)
POST = (Path(__file__).parents[1] / "fixtures/posts/it_recruiter.txt").read_text(encoding="utf-8")


@pytest.fixture
def workspace(tmp_path):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    upgrade_database(url)
    database = Database(url)
    try:
        yield database, url
    finally:
        database.close()


def observation(text=POST, **fields):
    values = dict(
        telegram_chat_id=CHAT,
        telegram_message_id=10,
        published_at=NOW,
        observed_at=NOW,
        full_text=text,
        content_kind="text",
    )
    return MessageObservation(**(values | fields))


def setup_message(database, versions=VERSIONS):
    store = MessageStore(database, versions)
    source = store.register_source(
        telegram_chat_id=CHAT, name="Synthetic channel", now=NOW, backfill_days=2
    )
    return store, source, store.record_observation(source.id, observation())


def test_fixture_to_selected_result_is_atomic_and_idempotent(workspace):
    database, _ = workspace
    _, _, saved = setup_message(database)
    first = process_pending(database, clock=lambda: NOW)
    second = process_pending(database, clock=lambda: NOW)
    assert (first.completed, first.selected, second.completed) == (1, 1, 0)
    with database.transaction() as session:
        revision = session.get(MessageRevision, saved.revision_id)
        run = session.scalars(select(ExtractionRun)).one()
        posting = session.scalars(select(JobPosting)).one()
        offer = session.scalars(select(SalaryOffer)).one()
        assert revision.active_extraction_run_id == run.id == saved.request_id
        assert run.status == "completed" and run.classification == "single_vacancy"
        assert posting.role_family_code == "recruitment_hr"
        assert offer.maximum_amount_scaled == 25_000_000 and offer.currency_code is None
        for quote in offer.evidence:
            assert POST[quote["start_offset"] : quote["end_offset"]] == quote["text"]


def active_id(database, revision_id):
    with database.transaction() as session:
        return session.get(MessageRevision, revision_id).active_extraction_run_id


def finish(queue, claim):
    result = parse_vacancy(claim.full_text, content_kind=claim.content_kind)
    vocabulary = tuple(
        KeywordEntry(k.canonical_code, k.display_name, k.category_code, k.description, k.active)
        for k in load_taxonomy().keywords
    )
    return queue.complete(claim, result, vocabulary, now=NOW)


def test_newer_completed_request_wins_regardless_of_completion_order(workspace):
    database, _ = workspace
    _, _, saved = setup_message(database)
    queue = ExtractionQueue(database)
    older = queue.claim_next(now=NOW)
    (new_id,) = queue.enqueue_reprocess([saved.revision_id, saved.revision_id], VERSIONS, now=NOW)
    newer = queue.claim_next(now=NOW)
    assert newer.id == new_id and newer.request_key != older.request_key
    assert active_id(database, saved.revision_id) is None
    assert finish(queue, newer)
    assert not finish(queue, older)
    with pytest.raises(StaleClaim):
        finish(queue, older)
    assert active_id(database, saved.revision_id) == newer.id
    assert queue.get_request(older.id).status == "completed"
    with database.transaction() as session:
        assert len(session.scalars(select(JobPosting)).all()) == 2
        codes = session.scalars(select(Keyword.canonical_code)).all()
        assert len(codes) == len(set(codes)) and "linkedin" in codes


def test_late_old_revision_result_does_not_select_or_fill_new_revision(workspace):
    database, _ = workspace
    store, source, saved = setup_message(database)
    queue = ExtractionQueue(database)
    old_claim = queue.claim_next(now=NOW)
    edited = store.record_observation(
        source.id,
        observation(
            "Vacancy: QA Engineer\nRequirements: Python",
            telegram_edited_at=NOW + timedelta(seconds=1),
        ),
    )
    assert not finish(queue, old_claim)
    assert queue.get_request(old_claim.id).status == "completed"
    assert active_id(database, saved.revision_id) is None
    assert active_id(database, edited.revision_id) is None
    assert finish(queue, queue.claim_next(now=NOW))
    assert active_id(database, edited.revision_id) == edited.request_id


def test_manual_selection_survives_automatic_reprocess_but_not_new_revision(workspace):
    database, _ = workspace
    store, source, saved = setup_message(database)
    process_pending(database, clock=lambda: NOW)
    with database.transaction() as session:
        manual = ExtractionRun(
            message_revision_id=saved.revision_id,
            request_key="test-manual",
            origin="manual",
            parent_run_id=saved.request_id,
            parser_version=VERSIONS.parser,
            taxonomy_version=VERSIONS.taxonomy,
            status="completed",
            classification="other",
            created_at=NOW,
            finished_at=NOW,
        )
        session.add(manual)
        session.flush()
        manual_id = manual.id
        session.get(MessageRevision, saved.revision_id).active_extraction_run_id = manual_id
    queue = ExtractionQueue(database)
    (rerun_id,) = queue.enqueue_reprocess([saved.revision_id], VERSIONS, now=NOW)
    summary = process_pending(database, clock=lambda: NOW)
    assert (summary.completed, summary.selected) == (1, 0)
    assert queue.get_request(rerun_id).status == "completed"
    assert active_id(database, saved.revision_id) == manual_id
    edited = store.record_observation(
        source.id,
        observation(
            telegram_edited_at=NOW + timedelta(seconds=1),
        ),
    )
    assert active_id(database, edited.revision_id) is None
    assert process_pending(database, clock=lambda: NOW).selected == 1
    assert active_id(database, edited.revision_id) == edited.request_id


def test_reclaimed_attempt_rejects_old_completion_and_failure(workspace):
    database, _ = workspace
    _, _, saved = setup_message(database)
    queue = ExtractionQueue(database)
    old = queue.claim_next(now=NOW)
    assert queue.recover_interrupted(now=NOW).requeued == 1
    new = queue.claim_next(now=NOW)
    assert old.id == new.id == saved.request_id and new.attempt_count == 2
    with pytest.raises(StaleClaim):
        finish(queue, old)
    with pytest.raises(StaleClaim):
        queue.record_failure(old, FailureReason.INVALID_RESULT, now=NOW)
    assert finish(queue, new)


def test_version_mismatch_is_terminal_without_invoking_parser(workspace):
    database, _ = workspace
    _, _, saved = setup_message(database, ExtractionVersions("missing-version", VERSIONS.taxonomy))

    def forbidden(*args, **kwargs):
        pytest.fail("An unavailable pinned version must not invoke the parser.")

    summary = process_pending(database, clock=lambda: NOW, parser=forbidden)
    request = ExtractionQueue(database).get_request(saved.request_id)
    assert (summary.failed, summary.delayed) == (1, 0)
    assert request.last_error == "version_unavailable" and request.attempt_count == 1
    assert active_id(database, saved.revision_id) is None


def test_invalid_evidence_and_parser_errors_fail_without_private_details(workspace, capsys):
    database, _ = workspace
    store, source, saved = setup_message(database)
    second = store.record_observation(source.id, observation(telegram_message_id=11))
    private = "synthetic private source"

    def parser(text, *, content_kind):
        result = parse_vacancy(text, content_kind=content_kind)
        if not hasattr(parser, "first"):
            parser.first = True
            return result.model_copy(update={"classification_evidence": (Evidence(text=private),)})
        raise RuntimeError(private)

    summary = process_pending(database, clock=lambda: NOW, parser=parser)
    queue = ExtractionQueue(database)
    assert summary.failed == 2
    assert queue.get_request(saved.request_id).last_error == "invalid_result"
    assert queue.get_request(second.request_id).last_error == "parser_error"
    assert private not in repr(summary) + repr(capsys.readouterr())
    assert active_id(database, saved.revision_id) is None


def test_failed_child_write_rolls_back_snapshot_and_keeps_previous_selection(workspace):
    database, _ = workspace
    _, _, saved = setup_message(database)
    process_pending(database, clock=lambda: NOW)
    queue = ExtractionQueue(database)
    (rerun,) = queue.enqueue_reprocess([saved.revision_id], VERSIONS, now=NOW)
    with database.engine.begin() as connection:
        connection.exec_driver_sql("""
            CREATE TRIGGER test_reject_offer BEFORE INSERT ON salary_offers
            BEGIN SELECT RAISE(ABORT, 'synthetic private failure'); END
        """)
    summary = process_pending(database, clock=lambda: NOW)
    assert (summary.failed, summary.completed) == (1, 0)
    assert queue.get_request(rerun).last_error == "storage_error"
    assert active_id(database, saved.revision_id) == saved.request_id
    with database.transaction() as session:
        assert session.scalars(select(JobPosting)).one().extraction_run_id == saved.request_id


def test_retry_survives_restart_and_skips_delayed_request(workspace):
    database, url = workspace
    store, source, saved = setup_message(database)
    queue = ExtractionQueue(database)
    first = queue.claim_next(now=NOW)
    assert queue.record_failure(first, FailureReason.STORAGE_BUSY, now=NOW) == "pending"
    second = store.record_observation(source.id, observation(telegram_message_id=11))
    database.close()
    restarted = Database(url)
    try:
        summary = process_pending(restarted, clock=lambda: NOW, recover=True)
        assert (summary.completed, summary.delayed) == (1, 1)
        assert active_id(restarted, second.revision_id) == second.request_id
        queue = ExtractionQueue(restarted)
        assert queue.get_request(saved.request_id).retry_not_before == NOW + timedelta(seconds=5)
        assert queue.claim_next(now=NOW + timedelta(seconds=4)) is None
        retry = queue.claim_next(now=NOW + timedelta(seconds=5))
        assert retry.id == first.id and retry.attempt_count == 2
        assert queue.record_failure(retry, FailureReason.STORAGE_BUSY, now=NOW) == "pending"
        last = queue.claim_next(now=NOW + timedelta(seconds=10))
        assert last.attempt_count == 3
        assert queue.record_failure(last, FailureReason.STORAGE_BUSY, now=NOW) == "failed"
        assert queue.claim_next(now=NOW + timedelta(days=1)) is None
    finally:
        restarted.close()


def test_worker_lock_prevents_cli_from_recovering_a_live_claim(workspace, tmp_path):
    database, url = workspace
    _, _, saved = setup_message(database)
    queue = ExtractionQueue(database)
    queue.claim_next(now=NOW)
    with lock_workspace(url):
        result = run_cli(tmp_path, "process-pending")
    assert result.returncode == 1
    assert "Another worker owns this database workspace" in result.stderr
    assert queue.get_request(saved.request_id).status == "processing"
    result = run_cli(tmp_path, "process-pending")
    assert result.returncode == 0
    assert "recovered: 1" in result.stdout and "Completed: 1" in result.stdout
    assert queue.get_request(saved.request_id).attempt_count == 2


def run_cli(root, *args):
    return subprocess.run(
        [sys.executable, "-m", "job_market", "--project-root", str(root), *args],
        cwd=root,
        env={**os.environ, "DATABASE_URL": "sqlite:///test.db"},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_offline_cli_reprocess_only_queues_explicit_revisions(workspace, tmp_path):
    database, _ = workspace
    _, _, saved = setup_message(database)
    assert run_cli(tmp_path, "process-pending").returncode == 0
    invalid = run_cli(tmp_path, "reprocess", str(saved.revision_id), "9000")
    assert invalid.returncode == 1 and "Revision 9000 does not exist" in invalid.stderr
    queued = run_cli(tmp_path, "reprocess", str(saved.revision_id))
    assert queued.returncode == 0
    request_id = int(queued.stdout.strip().split(": ")[-1])
    assert request_id == saved.request_id + 1
    assert ExtractionQueue(database).get_request(request_id).status == "pending"
    assert active_id(database, saved.revision_id) == saved.request_id
    finished = run_cli(tmp_path, "process-pending")
    assert finished.returncode == 0 and "Completed: 1" in finished.stdout
    assert active_id(database, saved.revision_id) == request_id


def test_upgrade_from_initial_schema_preserves_pending_rows(tmp_path):
    url = f"sqlite:///{tmp_path / 'previous.db'}"
    database = Database(url)
    try:
        with database.engine.begin() as connection, migration_config() as config:
            config.attributes["connection"] = connection
            command.upgrade(config, "0001_initial_schema")
            connection.exec_driver_sql("""
                INSERT INTO sources
                    (id, telegram_chat_id, name, source_type, history_start_at, created_at)
                VALUES (1, -1234, 'Synthetic', 'channel', '2026-11-12', '2026-11-12')
            """)
            connection.exec_driver_sql("""
                INSERT INTO messages (id, source_id, telegram_message_id,
                                      published_at, first_seen_at, last_seen_at)
                VALUES (1, 1, 1, '2026-11-12', '2026-11-12', '2026-11-12')
            """)
            connection.exec_driver_sql(
                """
                INSERT INTO message_revisions
                    (id, message_id, revision_number, full_text, content_kind,
                     observed_at, content_hash)
                VALUES (1, 1, 1, 'Vacancy: QA Engineer', 'text', '2026-11-12', ?)
            """,
                ("0" * 64,),
            )
            connection.exec_driver_sql("UPDATE messages SET current_revision_id = 1 WHERE id = 1")
            connection.exec_driver_sql(
                """
                INSERT INTO extraction_runs
                    (id, message_revision_id, request_key, origin, status, parser_version,
                     taxonomy_version, classification_evidence, created_at)
                VALUES (1, 1, 'automatic:revision:1', 'automatic', 'pending', ?, ?, '[]',
                        '2026-11-12')
            """,
                (VERSIONS.parser, VERSIONS.taxonomy),
            )
        upgrade_database(url)
        state = ExtractionQueue(database).get_request(1)
        assert state.status == "pending" and state.retry_not_before is None
        assert process_pending(database, clock=lambda: NOW).selected == 1
        assert active_id(database, 1) == 1
    finally:
        database.close()


def test_all_reference_posts_and_review_issues_survive_persistence(workspace):
    database, _ = workspace
    store, source, _ = setup_message(database)
    fixtures = Path(__file__).parents[1] / "fixtures/posts"
    for number, name in enumerate(("business_development_lead", "ua_manager"), start=11):
        store.record_observation(
            source.id,
            observation(
                (fixtures / f"{name}.txt").read_text(encoding="utf-8"),
                telegram_message_id=number,
            ),
        )
    ambiguous = store.record_observation(
        source.id,
        observation(
            "Vacancy: QA Engineer\nSalary: €2.500",
            telegram_message_id=13,
        ),
    )
    summary = process_pending(database, clock=lambda: NOW)
    assert (summary.completed, summary.selected) == (4, 4)
    with database.transaction() as session:
        assert set(session.scalars(select(JobPosting.role_family_code))) == {
            "recruitment_hr",
            "marketing_growth",
            "sales_business_development",
            "quality_assurance",
        }
        mention = session.scalars(
            select(KeywordMention)
            .join(Keyword, KeywordMention.keyword_id == Keyword.id)
            .where(Keyword.canonical_code == "appsflyer")
        ).one()
        assert (
            mention.original_term == "AppsFlyer" and mention.qualification_level == "understanding"
        )
        issue = session.scalars(
            select(ReviewIssue).where(
                ReviewIssue.extraction_run_id == ambiguous.request_id,
            )
        ).one()
        assert (issue.message_revision_id, issue.affected_metric, issue.status) == (
            ambiguous.revision_id,
            "salary",
            "open",
        )
        assert session.get(ExtractionRun, ambiguous.request_id).review_status == "needs_review"
