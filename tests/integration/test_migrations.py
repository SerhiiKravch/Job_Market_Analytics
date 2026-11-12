"""Exercise actual Alembic upgrades and relational constraints on SQLite."""

import hashlib
import io
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from alembic import command
from sqlalchemy import delete, func, inspect, select, update
from sqlalchemy.exc import IntegrityError, StatementError

from job_market.domain.validation import INT64_MAX, scale_money, unscale_money
from job_market.storage.database import BUSY_TIMEOUT_MS, Database
from job_market.storage.migration import migration_config, upgrade_database
from job_market.storage.models import (
    DuplicateLink,
    ExperienceRequirement,
    ExtractionRun,
    JobPosting,
    Keyword,
    KeywordMention,
    Message,
    MessageRevision,
    ReviewIssue,
    SalaryOffer,
    Source,
)

NOW = datetime(2026, 10, 18, 7, 1, 2, 345678, tzinfo=timezone(timedelta(hours=2)))
ORIGINAL = "📈 Example vacancy — requirements"
DIGEST = hashlib.sha256(ORIGINAL.encode("utf-8")).hexdigest()
TABLES = {
    "sources",
    "messages",
    "message_revisions",
    "extraction_runs",
    "job_postings",
    "experience_requirements",
    "salary_offers",
    "keywords",
    "keyword_mentions",
    "review_issues",
    "duplicate_links",
}


@pytest.fixture
def database(tmp_path):
    url = f"sqlite:///{tmp_path / 'runtime' / 'test.db'}"
    upgrade_database(url)
    db = Database(url)
    try:
        yield db, url
    finally:
        db.close()


def _add(session, model, **fields):
    row = model(**fields)
    session.add(row)
    session.flush()
    return row


def _source(session, **fields):
    return _add(
        session,
        Source,
        **(
            dict(
                telegram_chat_id=-9_000_000_000_000,
                name="Synthetic channel",
                source_type="channel",
                history_start_at=NOW,
                created_at=NOW,
            )
            | fields
        ),
    )


def _message(session, source_id, number):
    return _add(
        session,
        Message,
        source_id=source_id,
        telegram_message_id=number,
        published_at=NOW,
        first_seen_at=NOW,
        last_seen_at=NOW,
    )


def _revision(session, message_id, number):
    return _add(
        session,
        MessageRevision,
        message_id=message_id,
        revision_number=number,
        full_text=ORIGINAL,
        content_kind="text",
        observed_at=NOW,
        content_hash=DIGEST,
    )


def _run(session, revision_id, request_key, **fields):
    return _add(
        session,
        ExtractionRun,
        **(
            dict(
                message_revision_id=revision_id,
                request_key=request_key,
                origin="automatic",
                parser_version="test-1",
                taxonomy_version="1.0.0",
                status="completed",
                classification="single_vacancy",
                created_at=NOW,
            )
            | fields
        ),
    )


@pytest.fixture
def graph(database):
    db, _ = database
    with db.transaction() as session:
        source = _source(session)
        message = _message(session, source.id, 2**40)
        other_message = _message(session, source.id, 2**40 + 1)
        revision = _revision(session, message.id, 1)
        older_revision = _revision(session, message.id, 2)
        other_revision = _revision(session, other_message.id, 1)
        run = _run(session, revision.id, "complete")
        other_run = _run(session, other_revision.id, "other")
        pending = _run(session, revision.id, "pending", status="pending", classification=None)
        posting = _add(
            session,
            JobPosting,
            extraction_run_id=run.id,
            posting_number=1,
            role_family_code="unclassified",
        )
        other_posting = _add(
            session,
            JobPosting,
            extraction_run_id=other_run.id,
            posting_number=1,
            role_family_code="unclassified",
        )
        keyword = _add(
            session,
            Keyword,
            canonical_code="example",
            display_name="Example",
            category_code="technology",
        )
        message.current_revision_id = revision.id
        revision.active_extraction_run_id = run.id
        return SimpleNamespace(
            source=source.id,
            message=message.id,
            other_message=other_message.id,
            revision=revision.id,
            same_message_revision=older_revision.id,
            other_revision=other_revision.id,
            run=run.id,
            other_run=other_run.id,
            pending=pending.id,
            posting=posting.id,
            other_posting=other_posting.id,
            keyword=keyword.id,
        )


def test_fresh_upgrade_has_all_tables_and_connection_policy(database):
    db, _ = database
    with db.engine.connect() as first, db.engine.connect() as second:
        schema = inspect(first)
        assert set(schema.get_table_names()) == TABLES | {"alembic_version"}
        for table in TABLES:
            assert schema.get_pk_constraint(table)["constrained_columns"] == ["id"]
            id_column = next(
                column for column in schema.get_columns(table) if column["name"] == "id"
            )
            assert str(id_column["type"]) == "INTEGER"
        assert first.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == (
            "0002_extraction_retry"
        )
        for connection in (first, second):
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == BUSY_TIMEOUT_MS
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_migration_and_metadata_agree_and_rerun_preserves_records(database, graph):
    db, url = database
    upgrade_database(url)
    with db.engine.begin() as connection, migration_config() as config:
        config.attributes["connection"] = connection
        command.check(config)
        assert connection.scalar(select(func.count()).select_from(MessageRevision)) == 3
        assert (
            connection.scalar(
                select(Message.current_revision_id).where(Message.id == graph.message)
            )
            == graph.revision
        )
        assert (
            connection.scalar(
                select(MessageRevision.active_extraction_run_id).where(
                    MessageRevision.id == graph.revision
                )
            )
            == graph.run
        )
        assert (
            connection.scalar(select(ExtractionRun.status).where(ExtractionRun.id == graph.pending))
            == "pending"
        )
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_message_cannot_select_another_messages_revision(database, graph):
    db, _ = database
    with pytest.raises(IntegrityError), db.transaction() as session:
        session.execute(
            update(Message)
            .where(Message.id == graph.message)
            .values(
                current_revision_id=graph.other_revision,
            )
        )


@pytest.mark.parametrize("run_attribute", ["other_run", "pending"])
def test_revision_can_only_select_its_own_completed_run(database, graph, run_attribute):
    db, _ = database
    with pytest.raises(IntegrityError), db.transaction() as session:
        session.execute(
            update(MessageRevision)
            .where(MessageRevision.id == graph.revision)
            .values(active_extraction_run_id=getattr(graph, run_attribute))
        )
    with db.transaction() as session:
        assert session.get(MessageRevision, graph.revision).active_extraction_run_id == graph.run


def test_selected_run_cannot_be_demoted_or_use_a_different_discriminator(database, graph):
    db, _ = database
    with pytest.raises(IntegrityError), db.transaction() as session:
        session.execute(
            update(ExtractionRun)
            .where(ExtractionRun.id == graph.run)
            .values(
                status="pending",
                classification=None,
            )
        )
    with pytest.raises(IntegrityError), db.transaction() as session:
        session.execute(
            update(MessageRevision)
            .where(MessageRevision.id == graph.revision)
            .values(
                active_extraction_run_id=graph.pending,
                active_run_status="pending",
            )
        )


def test_no_orphans_or_accidental_history_cascade(database, graph):
    db, _ = database
    with pytest.raises(IntegrityError), db.transaction() as session:
        _message(session, 9999, 20)
    with pytest.raises(IntegrityError), db.transaction() as session:
        session.execute(delete(Source).where(Source.id == graph.source))
    with db.transaction() as session:
        assert session.get(Message, graph.message) is not None


@pytest.mark.parametrize(
    "duplicate", ["source", "message", "revision", "request", "posting", "keyword"]
)
def test_identity_and_request_uniqueness(database, graph, duplicate):
    db, _ = database
    with pytest.raises(IntegrityError), db.transaction() as session:
        match duplicate:
            case "source":
                _source(session)
            case "message":
                _message(session, graph.source, 2**40)
            case "revision":
                _revision(session, graph.message, 1)
            case "request":
                _run(session, graph.other_revision, "complete")
            case "posting":
                _add(
                    session,
                    JobPosting,
                    extraction_run_id=graph.run,
                    posting_number=1,
                    role_family_code="unclassified",
                )
            case "keyword":
                _add(
                    session,
                    Keyword,
                    canonical_code="example",
                    display_name="Another",
                    category_code="technology",
                )


def test_manual_parent_and_review_targets_must_share_ownership(database, graph):
    db, _ = database
    with pytest.raises(IntegrityError), db.transaction() as session:
        _run(session, graph.revision, "manual", origin="manual", parent_run_id=graph.other_run)
    with db.transaction() as session:
        corrected = _run(
            session, graph.revision, "manual", origin="manual", parent_run_id=graph.run
        )
        corrected_id = corrected.id
    issue = dict(
        message_revision_id=graph.revision,
        extraction_run_id=graph.run,
        job_posting_id=graph.posting,
        field_path="posting.salary_offers",
        issue_code="ambiguous_salary",
        description="Synthetic review issue",
        impact="blocks_metric",
        affected_metric="salary",
        created_at=NOW,
        resolution_run_id=corrected_id,
    )
    for invalid in (
        {"message_revision_id": graph.other_revision},
        {"job_posting_id": graph.other_posting},
        {"resolution_run_id": graph.other_run},
        {"affected_metric": None},
    ):
        with pytest.raises(IntegrityError), db.transaction() as session:
            _add(session, ReviewIssue, **(issue | invalid))
    with db.transaction() as session:
        _add(session, ReviewIssue, **issue)


def test_repost_pair_order_uniqueness_and_distinct_messages_are_enforced(database, graph):
    db, _ = database
    pair = dict(
        left_revision_id=graph.revision,
        right_revision_id=graph.other_revision,
        left_message_id=graph.message,
        right_message_id=graph.other_message,
        method="exact_content",
        status="candidate",
        created_at=NOW,
    )
    for invalid in (
        {"right_revision_id": graph.same_message_revision, "right_message_id": graph.message},
        {"right_message_id": 9999},
        {
            "left_revision_id": graph.other_revision,
            "right_revision_id": graph.revision,
            "left_message_id": graph.other_message,
            "right_message_id": graph.message,
        },
    ):
        with pytest.raises(IntegrityError), db.transaction() as session:
            _add(session, DuplicateLink, **(pair | invalid))
    with db.transaction() as session:
        _add(session, DuplicateLink, **pair)
    with pytest.raises(IntegrityError), db.transaction() as session:
        _add(session, DuplicateLink, **pair)


def _salary(graph, **fields):
    return (
        dict(
            job_posting_id=graph.posting,
            bound_type="upper_only",
            maximum_amount_scaled=25_000_000,
            currency_symbol="$",
            payment_period="unspecified",
            tax_basis="net",
            compensation_component="unspecified",
            evidence=[{"text": ORIGINAL}],
        )
        | fields
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"maximum_amount_scaled": None},
        {"maximum_amount_scaled": -1},
        {"maximum_amount_scaled": 1.5},
        {"minimum_amount_scaled": 0},
        {"bound_type": "exact", "minimum_amount_scaled": 1},
        {"bound_type": "range", "minimum_amount_scaled": 26_000_000},
        {"currency_code": "usd"},
        {"currency_code": "ÁBC"},
        {"payment_period": "fortnight"},
    ],
)
def test_database_rejects_invalid_money_and_unrecognized_codes(database, graph, fields):
    db, _ = database
    with pytest.raises(IntegrityError), db.transaction() as session:
        _add(session, SalaryOffer, **_salary(graph, **fields))


def test_experience_and_evidence_offsets_have_database_constraints(database, graph):
    db, _ = database
    requirement = dict(
        job_posting_id=graph.posting,
        scope_type="overall",
        scope_text="Example requirement",
        requirement_context="required",
        prior_experience_policy="required",
        original_text=ORIGINAL,
    )
    for fields in (
        {"minimum_months": -1},
        {"minimum_months": 1.5},
        {"minimum_months": 36, "maximum_months": 12},
        {"minimum_months": 0, "prior_experience_policy": "explicitly_not_required"},
    ):
        with pytest.raises(IntegrityError), db.transaction() as session:
            _add(session, ExperienceRequirement, **(requirement | fields))
    mention = dict(
        job_posting_id=graph.posting,
        keyword_id=graph.keyword,
        original_term="Example",
        context="required",
        qualification_level="unspecified",
        evidence_text="Example",
    )
    for fields in ({"start_offset": 2}, {"start_offset": 2, "end_offset": 1}):
        with pytest.raises(IntegrityError), db.transaction() as session:
            _add(session, KeywordMention, **(mention | fields))
    with db.transaction() as session:
        row = _add(session, ExperienceRequirement, **requirement)
        assert row.minimum_months is row.maximum_months is None
        start = ORIGINAL.index("Example")
        _add(session, KeywordMention, **mention, start_offset=start, end_offset=start + 7)


def test_signed_ids_utc_unicode_evidence_and_scaled_money_round_trip(database, graph):
    db, _ = database
    with db.transaction() as session:
        _add(
            session,
            SalaryOffer,
            **_salary(
                graph,
                maximum_amount_scaled=scale_money("922337203685477.5807"),
            ),
        )
    with db.transaction() as session:
        source = session.get(Source, graph.source)
        message = session.get(Message, graph.message)
        revision = session.get(MessageRevision, graph.revision)
        offer = session.scalars(select(SalaryOffer)).one()
        assert source.telegram_chat_id == -9_000_000_000_000
        assert message.telegram_message_id == 2**40
        assert message.published_at == NOW.astimezone(UTC)
        assert message.published_at.tzinfo is UTC
        assert revision.full_text == ORIGINAL
        assert revision.content_hash == DIGEST
        assert offer.evidence == [{"text": ORIGINAL}]
        assert offer.maximum_amount_scaled == INT64_MAX
        assert str(unscale_money(offer.maximum_amount_scaled)) == "922337203685477.5807"
        assert offer.minimum_amount_scaled is offer.currency_code is None
    with db.engine.connect() as connection:
        stored = connection.exec_driver_sql(
            "SELECT published_at FROM messages ORDER BY id"
        ).scalar()
        assert stored == "2026-10-18 05:01:02.345678"


def test_failed_transaction_rolls_back_and_naive_time_is_rejected(database):
    db, _ = database
    with pytest.raises(StatementError, match="timezone-aware"), db.transaction() as session:
        _source(session, created_at=NOW.replace(tzinfo=None))
    with pytest.raises(IntegrityError), db.transaction() as session:
        _source(session)
        _source(session)
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Source)) == 0


def test_downgrade_of_disposable_database_keeps_foreign_keys_enabled(database, graph):
    db, _ = database
    with db.transaction() as session:
        _run(session, graph.revision, "manual", origin="manual", parent_run_id=graph.run)
    with db.engine.begin() as connection, migration_config() as config:
        config.attributes["connection"] = connection
        command.downgrade(config, "base")
        assert inspect(connection).get_table_names() == ["alembic_version"]
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


def test_postgresql_offline_ddl_has_portable_types_and_defers_circular_foreign_keys():
    with migration_config() as config:
        output = io.StringIO()
        config.output_buffer = output
        config.attributes["database_url"] = "postgresql://localhost/jobs"
        command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert all(f"CREATE TABLE {table} (" in sql for table in TABLES)
    assert "id BIGSERIAL NOT NULL" in sql
    assert "published_at TIMESTAMP WITH TIME ZONE NOT NULL" in sql
    assert "maximum_amount_scaled BIGINT" in sql
    for fragment in (
        "ALTER TABLE messages ADD CONSTRAINT fk_messages_current_revision",
        "ALTER TABLE message_revisions ADD CONSTRAINT fk_revisions_active_run",
    ):
        assert sql.index(fragment) > sql.index("CREATE TABLE extraction_runs (")


def test_installed_cli_upgrades_project_root_database_without_credentials(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text(
        "DATABASE_URL=sqlite:///var/offline.db\n"
        "TELEGRAM_API_ID=YOUR_NUMERIC_API_ID\nTELEGRAM_API_HASH=YOUR_API_HASH\n",
        encoding="utf-8",
    )
    arguments = [
        sys.executable,
        "-m",
        "job_market",
        "--project-root",
        str(project),
        "db",
        "upgrade",
    ]
    for _ in range(2):
        result = subprocess.run(
            arguments,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "Database schema is up to date." in result.stdout
    assert (project / "var/offline.db").is_file()
    assert not (tmp_path / "var").exists()
    assert not list(project.rglob("*.session"))


def test_cli_migration_error_does_not_echo_database_path(tmp_path):
    blocked = "synthetic-private-path"
    (tmp_path / blocked).touch()
    (tmp_path / ".env").write_text(f"DATABASE_URL=sqlite:///{blocked}/jobs.db\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "job_market", "db", "upgrade"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 1
    assert "Check the configured database path, permissions" in result.stderr
    assert blocked not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr
