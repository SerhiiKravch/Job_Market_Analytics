"""Portable SQLAlchemy metadata for the eleven MVP tables."""

from datetime import datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from job_market.domain import enums
from job_market.storage.database import InternalId, UtcDateTime


class Base(DeclarativeBase):
    metadata = sa.MetaData(
        naming_convention={
            "pk": "pk_%(table_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        }
    )


class Identity:
    # The PK participates in the outbound ownership FK on messages and revisions.
    id: Mapped[int] = mapped_column(InternalId, primary_key=True, autoincrement="ignore_fk")


def _codes(column: str, enum: type[StrEnum]) -> sa.CheckConstraint:
    values = ", ".join(f"'{value}'" for value in enum)
    return sa.CheckConstraint(f"{column} IN ({values})", name=f"ck_{column}_code")


def _positive(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} > 0", name=f"ck_{column}_positive")


def _integer(column: str) -> sa.CheckConstraint:
    # SQLite's integer affinity alone also accepts REAL and TEXT.
    return sa.CheckConstraint(f"{column} = CAST({column} AS BIGINT)", name=f"ck_{column}_integer")


def _nonnegative(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} >= 0", name=f"ck_{column}_nonnegative")


def _bool(column: str) -> sa.Boolean:
    return sa.Boolean(create_constraint=True, name=f"ck_{column}_boolean")


_ASCII_UPPER = ", ".join(repr(letter) for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ")


class Source(Identity, Base):
    __tablename__ = "sources"
    __table_args__ = (
        sa.CheckConstraint("source_type = 'channel'", name="ck_sources_channel"),
        _integer("telegram_chat_id"),
        _integer("history_cursor_message_id"),
        _integer("live_checkpoint_message_id"),
        _positive("history_cursor_message_id"),
        _positive("live_checkpoint_message_id"),
    )

    telegram_chat_id: Mapped[int] = mapped_column(sa.BigInteger, unique=True)
    name: Mapped[str]
    username: Mapped[str | None]
    source_type: Mapped[str]
    enabled: Mapped[bool] = mapped_column(_bool("enabled"), server_default=sa.true())
    history_start_at: Mapped[datetime] = mapped_column(UtcDateTime)
    history_cursor_message_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    history_completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    live_checkpoint_message_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    last_successful_sync_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


class Message(Identity, Base):
    __tablename__ = "messages"
    __table_args__ = (
        sa.UniqueConstraint("source_id", "telegram_message_id"),
        sa.ForeignKeyConstraint(
            ["id", "current_revision_id"],
            ["message_revisions.message_id", "message_revisions.id"],
            name="fk_messages_current_revision",
            use_alter=True,
        ),
        sa.Index("ix_messages_source_published", "source_id", "published_at"),
        _positive("telegram_message_id"),
        _integer("telegram_message_id"),
    )

    source_id: Mapped[int] = mapped_column(InternalId, sa.ForeignKey("sources.id"))
    telegram_message_id: Mapped[int] = mapped_column(sa.BigInteger)
    published_at: Mapped[datetime] = mapped_column(UtcDateTime)
    first_seen_at: Mapped[datetime] = mapped_column(UtcDateTime)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime)
    message_url: Mapped[str | None]
    current_revision_id: Mapped[int | None] = mapped_column(InternalId)


class MessageRevision(Identity, Base):
    __tablename__ = "message_revisions"
    __table_args__ = (
        sa.UniqueConstraint("message_id", "revision_number", name="uq_revisions_message_number"),
        sa.UniqueConstraint("message_id", "id", name="uq_revisions_message_id"),
        sa.ForeignKeyConstraint(
            ["id", "active_extraction_run_id", "active_run_status"],
            ["extraction_runs.message_revision_id", "extraction_runs.id", "extraction_runs.status"],
            name="fk_revisions_active_run",
            use_alter=True,
        ),
        sa.CheckConstraint("active_run_status = 'completed'", name="ck_active_run_completed"),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_content_hash_length"),
        sa.Index("ix_revisions_content_hash", "content_hash"),
        _positive("revision_number"),
        _integer("revision_number"),
        _codes("content_kind", enums.ContentKind),
    )

    message_id: Mapped[int] = mapped_column(InternalId, sa.ForeignKey("messages.id"))
    revision_number: Mapped[int]
    full_text: Mapped[str] = mapped_column(sa.Text)
    content_kind: Mapped[str]
    telegram_edited_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime)
    content_hash: Mapped[str] = mapped_column(sa.String(64))
    active_extraction_run_id: Mapped[int | None] = mapped_column(InternalId)
    # A fixed discriminator lets the FK require completed runs on both dialects.
    active_run_status: Mapped[str] = mapped_column(server_default="completed")


class ExtractionRun(Identity, Base):
    __tablename__ = "extraction_runs"
    __table_args__ = (
        sa.UniqueConstraint("message_revision_id", "id", name="uq_runs_revision_id"),
        sa.UniqueConstraint("message_revision_id", "id", "status", name="uq_runs_revision_status"),
        sa.ForeignKeyConstraint(
            ["message_revision_id", "parent_run_id"],
            ["extraction_runs.message_revision_id", "extraction_runs.id"],
            name="fk_runs_parent",
        ),
        sa.CheckConstraint("parent_run_id <> id", name="ck_runs_no_self_parent"),
        sa.CheckConstraint(
            "(status = 'completed' AND classification IS NOT NULL) OR "
            "(status <> 'completed' AND classification IS NULL)",
            name="ck_runs_classification_on_completion",
        ),
        sa.CheckConstraint(
            "origin <> 'manual' OR (parent_run_id IS NOT NULL AND status = 'completed')",
            name="ck_runs_manual_snapshot",
        ),
        _codes("origin", enums.ExtractionOrigin),
        _codes("status", enums.ExtractionStatus),
        _codes("classification", enums.Classification),
        _codes("review_status", enums.ReviewStatus),
        _nonnegative("attempt_count"),
        _integer("attempt_count"),
        sa.Index("ix_runs_status_id", "status", "id"),
        sa.Index("ix_runs_revision_parent", "message_revision_id", "parent_run_id"),
    )

    message_revision_id: Mapped[int] = mapped_column(
        InternalId, sa.ForeignKey("message_revisions.id")
    )
    request_key: Mapped[str] = mapped_column(unique=True)
    origin: Mapped[str]
    parent_run_id: Mapped[int | None] = mapped_column(InternalId)
    parser_version: Mapped[str]
    taxonomy_version: Mapped[str]
    status: Mapped[str]
    classification: Mapped[str | None]
    classification_evidence: Mapped[list[dict[str, Any]]] = mapped_column(sa.JSON, default=list)
    review_status: Mapped[str] = mapped_column(server_default="not_required")
    attempt_count: Mapped[int] = mapped_column(server_default=sa.text("0"))
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    retry_not_before: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    reviewer: Mapped[str | None]
    change_reason: Mapped[str | None] = mapped_column(sa.Text)


class JobPosting(Identity, Base):
    __tablename__ = "job_postings"
    __table_args__ = (
        sa.UniqueConstraint("extraction_run_id", "posting_number", name="uq_postings_run_number"),
        sa.UniqueConstraint("extraction_run_id", "id", name="uq_postings_run_id"),
        _positive("posting_number"),
        _integer("posting_number"),
        _codes("role_family_code", enums.RoleFamily),
        _codes("seniority_code", enums.Seniority),
        _codes("work_arrangement", enums.WorkArrangement),
        _codes("employment_schedule", enums.EmploymentSchedule),
        _codes("organizational_arrangement", enums.OrganizationalArrangement),
        sa.Index("ix_postings_role_family", "role_family_code"),
    )

    extraction_run_id: Mapped[int] = mapped_column(InternalId, sa.ForeignKey("extraction_runs.id"))
    posting_number: Mapped[int]
    title_original: Mapped[str | None]
    role_normalized: Mapped[str | None]
    role_family_code: Mapped[str]
    seniority_original: Mapped[str | None]
    seniority_code: Mapped[str | None]
    work_arrangement: Mapped[str | None]
    employment_schedule: Mapped[str | None]
    organizational_arrangement: Mapped[str | None]
    compensation_text: Mapped[str | None] = mapped_column(sa.Text)
    fixed_pay_stated: Mapped[bool] = mapped_column(
        _bool("fixed_pay_stated"), server_default=sa.false()
    )
    bonus_stated: Mapped[bool] = mapped_column(_bool("bonus_stated"), server_default=sa.false())
    commission_stated: Mapped[bool] = mapped_column(
        _bool("commission_stated"), server_default=sa.false()
    )
    field_evidence: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)
    field_states: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)


class Keyword(Identity, Base):
    __tablename__ = "keywords"
    __table_args__ = (_codes("category_code", enums.KeywordCategory),)

    canonical_code: Mapped[str] = mapped_column(unique=True)
    display_name: Mapped[str]
    category_code: Mapped[str]
    description: Mapped[str | None] = mapped_column(sa.Text)
    active: Mapped[bool] = mapped_column(_bool("active"), server_default=sa.true())


class ExperienceRequirement(Identity, Base):
    __tablename__ = "experience_requirements"
    __table_args__ = (
        _codes("scope_type", enums.ExperienceScope),
        _codes("requirement_context", enums.RequirementContext),
        _codes("prior_experience_policy", enums.PriorExperiencePolicy),
        _nonnegative("minimum_months"),
        _nonnegative("maximum_months"),
        _integer("minimum_months"),
        _integer("maximum_months"),
        sa.CheckConstraint("minimum_months <= maximum_months", name="ck_experience_bounds_ordered"),
        sa.CheckConstraint(
            "prior_experience_policy <> 'explicitly_not_required' OR "
            "(minimum_months IS NULL AND maximum_months IS NULL)",
            name="ck_experience_no_duration_if_not_required",
        ),
        sa.Index("ix_experience_posting", "job_posting_id"),
        sa.Index("ix_experience_keyword", "keyword_id"),
    )

    job_posting_id: Mapped[int] = mapped_column(InternalId, sa.ForeignKey("job_postings.id"))
    scope_type: Mapped[str]
    scope_text: Mapped[str] = mapped_column(sa.Text)
    keyword_id: Mapped[int | None] = mapped_column(InternalId, sa.ForeignKey("keywords.id"))
    minimum_months: Mapped[int | None] = mapped_column(sa.BigInteger)
    maximum_months: Mapped[int | None] = mapped_column(sa.BigInteger)
    requirement_context: Mapped[str]
    prior_experience_policy: Mapped[str]
    original_text: Mapped[str] = mapped_column(sa.Text)
    field_states: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)


class SalaryOffer(Identity, Base):
    __tablename__ = "salary_offers"
    __table_args__ = (
        _codes("bound_type", enums.SalaryBound),
        _codes("payment_period", enums.PaymentPeriod),
        _codes("tax_basis", enums.TaxBasis),
        _codes("compensation_component", enums.CompensationComponent),
        _nonnegative("minimum_amount_scaled"),
        _nonnegative("maximum_amount_scaled"),
        _integer("minimum_amount_scaled"),
        _integer("maximum_amount_scaled"),
        sa.CheckConstraint(
            "(bound_type = 'exact' AND minimum_amount_scaled IS NOT NULL AND "
            "maximum_amount_scaled IS NOT NULL AND minimum_amount_scaled = maximum_amount_scaled) "
            "OR "
            "(bound_type = 'range' AND minimum_amount_scaled IS NOT NULL AND "
            "maximum_amount_scaled IS NOT NULL AND minimum_amount_scaled <= maximum_amount_scaled) "
            "OR (bound_type = 'lower_only' AND minimum_amount_scaled IS NOT NULL "
            "AND maximum_amount_scaled IS NULL) "
            "OR (bound_type = 'upper_only' AND minimum_amount_scaled IS NULL "
            "AND maximum_amount_scaled IS NOT NULL)",
            name="ck_salary_bound_shape",
        ),
        sa.CheckConstraint(
            "length(currency_code) = 3 AND "
            + " AND ".join(
                f"substr(currency_code, {i}, 1) IN ({_ASCII_UPPER})" for i in range(1, 4)
            ),
            name="ck_currency_code_ascii",
        ),
        sa.Index("ix_salary_posting", "job_posting_id"),
    )

    job_posting_id: Mapped[int] = mapped_column(InternalId, sa.ForeignKey("job_postings.id"))
    minimum_amount_scaled: Mapped[int | None] = mapped_column(sa.BigInteger)
    maximum_amount_scaled: Mapped[int | None] = mapped_column(sa.BigInteger)
    bound_type: Mapped[str]
    currency_symbol: Mapped[str | None]
    currency_code: Mapped[str | None] = mapped_column(sa.String(3))
    payment_period: Mapped[str]
    tax_basis: Mapped[str]
    compensation_component: Mapped[str]
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(sa.JSON)
    field_states: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)


class KeywordMention(Identity, Base):
    __tablename__ = "keyword_mentions"
    __table_args__ = (
        _codes("context", enums.MentionContext),
        _codes("qualification_level", enums.QualificationLevel),
        sa.CheckConstraint(
            "(start_offset IS NULL AND end_offset IS NULL) OR "
            "(start_offset IS NOT NULL AND end_offset IS NOT NULL AND "
            "start_offset >= 0 AND end_offset > start_offset)",
            name="ck_mention_offsets",
        ),
        _integer("start_offset"),
        _integer("end_offset"),
        sa.Index("ix_mentions_posting", "job_posting_id"),
        sa.Index("ix_mentions_keyword_context", "keyword_id", "context"),
    )

    job_posting_id: Mapped[int] = mapped_column(InternalId, sa.ForeignKey("job_postings.id"))
    keyword_id: Mapped[int] = mapped_column(InternalId, sa.ForeignKey("keywords.id"))
    original_term: Mapped[str]
    context: Mapped[str]
    qualification_level: Mapped[str]
    section_label: Mapped[str | None]
    evidence_text: Mapped[str] = mapped_column(sa.Text)
    start_offset: Mapped[int | None] = mapped_column(sa.BigInteger)
    end_offset: Mapped[int | None] = mapped_column(sa.BigInteger)


class ReviewIssue(Identity, Base):
    __tablename__ = "review_issues"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["message_revision_id", "extraction_run_id"],
            ["extraction_runs.message_revision_id", "extraction_runs.id"],
            name="fk_issues_run_revision",
        ),
        sa.ForeignKeyConstraint(
            ["message_revision_id", "resolution_run_id"],
            ["extraction_runs.message_revision_id", "extraction_runs.id"],
            name="fk_issues_resolution_revision",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_run_id", "job_posting_id"],
            ["job_postings.extraction_run_id", "job_postings.id"],
            name="fk_issues_posting_run",
        ),
        _codes("impact", enums.IssueImpact),
        _codes("affected_metric", enums.MetricCode),
        _codes("status", enums.IssueStatus),
        sa.CheckConstraint(
            "(impact = 'blocks_metric' AND affected_metric IS NOT NULL) OR "
            "(impact <> 'blocks_metric' AND affected_metric IS NULL)",
            name="ck_issue_metric_impact",
        ),
        sa.Index("ix_issues_run_status", "extraction_run_id", "status"),
        sa.Index("ix_issues_status", "status"),
        sa.Index("ix_issues_resolution_run", "resolution_run_id"),
    )

    # Shared scope key for the original and resolution run FKs.
    message_revision_id: Mapped[int] = mapped_column(InternalId)
    extraction_run_id: Mapped[int] = mapped_column(InternalId)
    job_posting_id: Mapped[int | None] = mapped_column(InternalId)
    field_path: Mapped[str] = mapped_column(sa.String(120))
    issue_code: Mapped[str]
    description: Mapped[str] = mapped_column(sa.Text)
    impact: Mapped[str]
    affected_metric: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default="open")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    resolution_run_id: Mapped[int | None] = mapped_column(InternalId)
    resolution_note: Mapped[str | None] = mapped_column(sa.Text)


class DuplicateLink(Identity, Base):
    __tablename__ = "duplicate_links"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["left_message_id", "left_revision_id"],
            ["message_revisions.message_id", "message_revisions.id"],
            name="fk_duplicates_left_revision",
        ),
        sa.ForeignKeyConstraint(
            ["right_message_id", "right_revision_id"],
            ["message_revisions.message_id", "message_revisions.id"],
            name="fk_duplicates_right_revision",
        ),
        sa.UniqueConstraint("left_revision_id", "right_revision_id", name="uq_duplicates_pair"),
        sa.CheckConstraint("left_revision_id < right_revision_id", name="ck_duplicates_pair_order"),
        sa.CheckConstraint(
            "left_message_id <> right_message_id", name="ck_duplicates_other_message"
        ),
        _codes("method", enums.DuplicateMethod),
        _codes("status", enums.DuplicateStatus),
        sa.Index("ix_duplicates_right_revision", "right_revision_id"),
    )

    left_revision_id: Mapped[int] = mapped_column(InternalId)
    right_revision_id: Mapped[int] = mapped_column(InternalId)
    # Ownership keys make the distinct-message rule enforceable in SQL.
    left_message_id: Mapped[int] = mapped_column(InternalId)
    right_message_id: Mapped[int] = mapped_column(InternalId)
    method: Mapped[str]
    status: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    reviewed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    note: Mapped[str | None] = mapped_column(sa.Text)
