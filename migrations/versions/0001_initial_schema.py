"""Create the portable Telegram job analytics schema."""

import sqlalchemy as sa
from alembic import op

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None

# Keep this revision independent of the application models and current taxonomy.
_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
_UTC_TIME = sa.DateTime(timezone=True).with_variant(sa.DateTime(), "sqlite")

_CURRENT_REVISION = (
    "fk_messages_current_revision",
    "messages",
    "message_revisions",
    ["id", "current_revision_id"],
    ["message_id", "id"],
)
_ACTIVE_RUN = (
    "fk_revisions_active_run",
    "message_revisions",
    "extraction_runs",
    ["id", "active_extraction_run_id", "active_run_status"],
    ["message_revision_id", "id", "status"],
)


def _current_revision_fk():
    name, _, remote, local_columns, remote_columns = _CURRENT_REVISION
    return sa.ForeignKeyConstraint(
        local_columns, [f"{remote}.{column}" for column in remote_columns], name=name
    )


def _active_run_fk():
    name, _, remote, local_columns, remote_columns = _ACTIVE_RUN
    return sa.ForeignKeyConstraint(
        local_columns, [f"{remote}.{column}" for column in remote_columns], name=name
    )


def upgrade() -> None:
    # SQLite accepts forward references only when declaring the constraint in
    # CREATE TABLE. PostgreSQL installs these two FKs after both tables exist.
    sqlite = op.get_context().dialect.name == "sqlite"
    op.create_table(
        "keywords",
        sa.Column("canonical_code", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("category_code", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "active",
            sa.Boolean(create_constraint=True, name="ck_active_boolean"),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column("id", _ID, autoincrement="ignore_fk", nullable=False),
        sa.CheckConstraint(
            (
                "category_code IN ('technology', 'tool_platform', 'generic_tool_categor"
                "y', 'skill_method', 'metric', 'industry_domain', 'product_service_cont"
                "ext', 'commercial_delivery_model')"
            ),
            name="ck_category_code_code",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_keywords")),
        sa.UniqueConstraint("canonical_code", name=op.f("uq_keywords_canonical_code")),
    )
    op.create_table(
        "sources",
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=True),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(create_constraint=True, name="ck_enabled_boolean"),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column("history_start_at", _UTC_TIME, nullable=False),
        sa.Column("history_cursor_message_id", sa.BigInteger(), nullable=True),
        sa.Column("history_completed_at", _UTC_TIME, nullable=True),
        sa.Column("live_checkpoint_message_id", sa.BigInteger(), nullable=True),
        sa.Column("last_successful_sync_at", _UTC_TIME, nullable=True),
        sa.Column("created_at", _UTC_TIME, nullable=False),
        sa.Column("id", _ID, autoincrement="ignore_fk", nullable=False),
        sa.CheckConstraint("source_type = 'channel'", name="ck_sources_channel"),
        sa.CheckConstraint(
            "history_cursor_message_id > 0", name="ck_history_cursor_message_id_positive"
        ),
        sa.CheckConstraint(
            "live_checkpoint_message_id > 0", name="ck_live_checkpoint_message_id_positive"
        ),
        sa.CheckConstraint(
            "history_cursor_message_id = CAST(history_cursor_message_id AS BIGINT)",
            name="ck_history_cursor_message_id_integer",
        ),
        sa.CheckConstraint(
            "live_checkpoint_message_id = CAST(live_checkpoint_message_id AS BIGINT)",
            name="ck_live_checkpoint_message_id_integer",
        ),
        sa.CheckConstraint(
            "telegram_chat_id = CAST(telegram_chat_id AS BIGINT)",
            name="ck_telegram_chat_id_integer",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sources")),
        sa.UniqueConstraint("telegram_chat_id", name=op.f("uq_sources_telegram_chat_id")),
    )
    op.create_table(
        "messages",
        sa.Column("source_id", _ID, nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=False),
        sa.Column("published_at", _UTC_TIME, nullable=False),
        sa.Column("first_seen_at", _UTC_TIME, nullable=False),
        sa.Column("last_seen_at", _UTC_TIME, nullable=False),
        sa.Column("message_url", sa.String(), nullable=True),
        sa.Column("current_revision_id", _ID, nullable=True),
        sa.Column("id", _ID, autoincrement="ignore_fk", nullable=False),
        sa.CheckConstraint(
            "telegram_message_id = CAST(telegram_message_id AS BIGINT)",
            name="ck_telegram_message_id_integer",
        ),
        sa.CheckConstraint("telegram_message_id > 0", name="ck_telegram_message_id_positive"),
        *([_current_revision_fk()] if sqlite else []),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], name=op.f("fk_messages_source_id_sources")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint("source_id", "telegram_message_id", name=op.f("uq_messages_source_id")),
    )
    op.create_index(
        "ix_messages_source_published", "messages", ["source_id", "published_at"], unique=False
    )
    op.create_table(
        "message_revisions",
        sa.Column("message_id", _ID, nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("full_text", sa.Text(), nullable=False),
        sa.Column("content_kind", sa.String(), nullable=False),
        sa.Column("telegram_edited_at", _UTC_TIME, nullable=True),
        sa.Column("observed_at", _UTC_TIME, nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("active_extraction_run_id", _ID, nullable=True),
        sa.Column("active_run_status", sa.String(), server_default="completed", nullable=False),
        sa.Column("id", _ID, autoincrement="ignore_fk", nullable=False),
        sa.CheckConstraint("active_run_status = 'completed'", name="ck_active_run_completed"),
        sa.CheckConstraint(
            "content_kind IN ('text', 'caption', 'empty', 'unsupported')",
            name="ck_content_kind_code",
        ),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_content_hash_length"),
        sa.CheckConstraint("revision_number > 0", name="ck_revision_number_positive"),
        sa.CheckConstraint(
            "revision_number = CAST(revision_number AS BIGINT)", name="ck_revision_number_integer"
        ),
        *([_active_run_fk()] if sqlite else []),
        sa.ForeignKeyConstraint(
            ["message_id"], ["messages.id"], name=op.f("fk_message_revisions_message_id_messages")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_revisions")),
        sa.UniqueConstraint("message_id", "id", name="uq_revisions_message_id"),
        sa.UniqueConstraint("message_id", "revision_number", name="uq_revisions_message_number"),
    )
    op.create_index(
        "ix_revisions_content_hash", "message_revisions", ["content_hash"], unique=False
    )
    op.create_table(
        "duplicate_links",
        sa.Column("left_revision_id", _ID, nullable=False),
        sa.Column("right_revision_id", _ID, nullable=False),
        sa.Column("left_message_id", _ID, nullable=False),
        sa.Column("right_message_id", _ID, nullable=False),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", _UTC_TIME, nullable=False),
        sa.Column("reviewed_at", _UTC_TIME, nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("id", _ID, autoincrement="ignore_fk", nullable=False),
        sa.CheckConstraint("method IN ('exact_content', 'manual')", name="ck_method_code"),
        sa.CheckConstraint(
            "status IN ('candidate', 'confirmed', 'rejected')", name="ck_status_code"
        ),
        sa.CheckConstraint(
            "left_message_id <> right_message_id", name="ck_duplicates_other_message"
        ),
        sa.CheckConstraint("left_revision_id < right_revision_id", name="ck_duplicates_pair_order"),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_duplicate_links")),
        sa.UniqueConstraint("left_revision_id", "right_revision_id", name="uq_duplicates_pair"),
    )
    op.create_index(
        "ix_duplicates_right_revision", "duplicate_links", ["right_revision_id"], unique=False
    )
    op.create_table(
        "extraction_runs",
        sa.Column("message_revision_id", _ID, nullable=False),
        sa.Column("request_key", sa.String(), nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("parent_run_id", _ID, nullable=True),
        sa.Column("parser_version", sa.String(), nullable=False),
        sa.Column("taxonomy_version", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("classification", sa.String(), nullable=True),
        sa.Column("classification_evidence", sa.JSON(), nullable=False),
        sa.Column("review_status", sa.String(), server_default="not_required", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", _UTC_TIME, nullable=False),
        sa.Column("started_at", _UTC_TIME, nullable=True),
        sa.Column("finished_at", _UTC_TIME, nullable=True),
        sa.Column("reviewer", sa.String(), nullable=True),
        sa.Column("change_reason", sa.Text(), nullable=True),
        sa.Column("id", _ID, autoincrement="ignore_fk", nullable=False),
        sa.CheckConstraint(
            (
                "(status = 'completed' AND classification IS NOT NULL) OR (status <> 'c"
                "ompleted' AND classification IS NULL)"
            ),
            name="ck_runs_classification_on_completion",
        ),
        sa.CheckConstraint(
            (
                "classification IN ('single_vacancy', 'multiple_vacancies', 'candidate'"
                ", 'other', 'uncertain')"
            ),
            name="ck_classification_code",
        ),
        sa.CheckConstraint(
            ("origin <> 'manual' OR (parent_run_id IS NOT NULL AND status = 'completed')"),
            name="ck_runs_manual_snapshot",
        ),
        sa.CheckConstraint("origin IN ('automatic', 'manual')", name="ck_origin_code"),
        sa.CheckConstraint(
            "review_status IN ('not_required', 'needs_review', 'reviewed')",
            name="ck_review_status_code",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')", name="ck_status_code"
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_attempt_count_nonnegative"),
        sa.CheckConstraint(
            "attempt_count = CAST(attempt_count AS BIGINT)", name="ck_attempt_count_integer"
        ),
        sa.CheckConstraint("parent_run_id <> id", name="ck_runs_no_self_parent"),
        sa.ForeignKeyConstraint(
            ["message_revision_id", "parent_run_id"],
            ["extraction_runs.message_revision_id", "extraction_runs.id"],
            name="fk_runs_parent",
        ),
        sa.ForeignKeyConstraint(
            ["message_revision_id"],
            ["message_revisions.id"],
            name=op.f("fk_extraction_runs_message_revision_id_message_revisions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extraction_runs")),
        sa.UniqueConstraint("message_revision_id", "id", "status", name="uq_runs_revision_status"),
        sa.UniqueConstraint("message_revision_id", "id", name="uq_runs_revision_id"),
        sa.UniqueConstraint("request_key", name=op.f("uq_extraction_runs_request_key")),
    )
    op.create_index(
        "ix_runs_revision_parent",
        "extraction_runs",
        ["message_revision_id", "parent_run_id"],
        unique=False,
    )
    op.create_index("ix_runs_status_id", "extraction_runs", ["status", "id"], unique=False)
    op.create_table(
        "job_postings",
        sa.Column("extraction_run_id", _ID, nullable=False),
        sa.Column("posting_number", sa.Integer(), nullable=False),
        sa.Column("title_original", sa.String(), nullable=True),
        sa.Column("role_normalized", sa.String(), nullable=True),
        sa.Column("role_family_code", sa.String(), nullable=False),
        sa.Column("seniority_original", sa.String(), nullable=True),
        sa.Column("seniority_code", sa.String(), nullable=True),
        sa.Column("work_arrangement", sa.String(), nullable=True),
        sa.Column("employment_schedule", sa.String(), nullable=True),
        sa.Column("organizational_arrangement", sa.String(), nullable=True),
        sa.Column("compensation_text", sa.Text(), nullable=True),
        sa.Column(
            "fixed_pay_stated",
            sa.Boolean(create_constraint=True, name="ck_fixed_pay_stated_boolean"),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "bonus_stated",
            sa.Boolean(create_constraint=True, name="ck_bonus_stated_boolean"),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "commission_stated",
            sa.Boolean(create_constraint=True, name="ck_commission_stated_boolean"),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("field_evidence", sa.JSON(), nullable=False),
        sa.Column("field_states", sa.JSON(), nullable=False),
        sa.Column("id", _ID, autoincrement="ignore_fk", nullable=False),
        sa.CheckConstraint(
            "employment_schedule IN ('full_time', 'part_time')", name="ck_employment_schedule_code"
        ),
        sa.CheckConstraint(
            "organizational_arrangement IN ('in_house', 'outstaff', 'outsource')",
            name="ck_organizational_arrangement_code",
        ),
        sa.CheckConstraint(
            (
                "role_family_code IN ('software_engineering', 'quality_assurance', 'inf"
                "rastructure_operations', 'data_ai', 'security', 'product_project_manag"
                "ement', 'business_analysis', 'design_ux', 'recruitment_hr', 'sales_bus"
                "iness_development', 'marketing_growth', 'customer_success_support', 'o"
                "ther_digital_business', 'unclassified')"
            ),
            name="ck_role_family_code_code",
        ),
        sa.CheckConstraint(
            (
                "seniority_code IN ('intern', 'trainee', 'junior', 'middle', 'senior', "
                "'lead', 'staff', 'principal')"
            ),
            name="ck_seniority_code_code",
        ),
        sa.CheckConstraint(
            "work_arrangement IN ('remote', 'hybrid', 'on_site')", name="ck_work_arrangement_code"
        ),
        sa.CheckConstraint("posting_number > 0", name="ck_posting_number_positive"),
        sa.CheckConstraint(
            "posting_number = CAST(posting_number AS BIGINT)", name="ck_posting_number_integer"
        ),
        sa.ForeignKeyConstraint(
            ["extraction_run_id"],
            ["extraction_runs.id"],
            name=op.f("fk_job_postings_extraction_run_id_extraction_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_postings")),
        sa.UniqueConstraint("extraction_run_id", "id", name="uq_postings_run_id"),
        sa.UniqueConstraint("extraction_run_id", "posting_number", name="uq_postings_run_number"),
    )
    op.create_index("ix_postings_role_family", "job_postings", ["role_family_code"], unique=False)
    op.create_table(
        "experience_requirements",
        sa.Column("job_posting_id", _ID, nullable=False),
        sa.Column("scope_type", sa.String(), nullable=False),
        sa.Column("scope_text", sa.Text(), nullable=False),
        sa.Column("keyword_id", _ID, nullable=True),
        sa.Column("minimum_months", sa.BigInteger(), nullable=True),
        sa.Column("maximum_months", sa.BigInteger(), nullable=True),
        sa.Column("requirement_context", sa.String(), nullable=False),
        sa.Column("prior_experience_policy", sa.String(), nullable=False),
        sa.Column("original_text", sa.Text(), nullable=False),
        sa.Column("field_states", sa.JSON(), nullable=False),
        sa.Column("id", _ID, autoincrement="ignore_fk", nullable=False),
        sa.CheckConstraint(
            (
                "prior_experience_policy <> 'explicitly_not_required' OR (minimum_month"
                "s IS NULL AND maximum_months IS NULL)"
            ),
            name="ck_experience_no_duration_if_not_required",
        ),
        sa.CheckConstraint(
            ("prior_experience_policy IN ('required', 'explicitly_not_required', 'unspecified')"),
            name="ck_prior_experience_policy_code",
        ),
        sa.CheckConstraint(
            "requirement_context IN ('required', 'preferred', 'unspecified')",
            name="ck_requirement_context_code",
        ),
        sa.CheckConstraint(
            (
                "scope_type IN ('overall', 'role_specific', 'technology_specific', 'dom"
                "ain_specific', 'other')"
            ),
            name="ck_scope_type_code",
        ),
        sa.CheckConstraint(
            "maximum_months = CAST(maximum_months AS BIGINT)", name="ck_maximum_months_integer"
        ),
        sa.CheckConstraint("maximum_months >= 0", name="ck_maximum_months_nonnegative"),
        sa.CheckConstraint("minimum_months <= maximum_months", name="ck_experience_bounds_ordered"),
        sa.CheckConstraint(
            "minimum_months = CAST(minimum_months AS BIGINT)", name="ck_minimum_months_integer"
        ),
        sa.CheckConstraint("minimum_months >= 0", name="ck_minimum_months_nonnegative"),
        sa.ForeignKeyConstraint(
            ["job_posting_id"],
            ["job_postings.id"],
            name=op.f("fk_experience_requirements_job_posting_id_job_postings"),
        ),
        sa.ForeignKeyConstraint(
            ["keyword_id"],
            ["keywords.id"],
            name=op.f("fk_experience_requirements_keyword_id_keywords"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_experience_requirements")),
    )
    op.create_index(
        "ix_experience_keyword", "experience_requirements", ["keyword_id"], unique=False
    )
    op.create_index(
        "ix_experience_posting", "experience_requirements", ["job_posting_id"], unique=False
    )
    op.create_table(
        "keyword_mentions",
        sa.Column("job_posting_id", _ID, nullable=False),
        sa.Column("keyword_id", _ID, nullable=False),
        sa.Column("original_term", sa.String(), nullable=False),
        sa.Column("context", sa.String(), nullable=False),
        sa.Column("qualification_level", sa.String(), nullable=False),
        sa.Column("section_label", sa.String(), nullable=True),
        sa.Column("evidence_text", sa.Text(), nullable=False),
        sa.Column("start_offset", sa.BigInteger(), nullable=True),
        sa.Column("end_offset", sa.BigInteger(), nullable=True),
        sa.Column("id", _ID, autoincrement="ignore_fk", nullable=False),
        sa.CheckConstraint(
            ("context IN ('required', 'preferred', 'responsibility', 'background_unspecified')"),
            name="ck_context_code",
        ),
        sa.CheckConstraint(
            (
                "qualification_level IN ('understanding', 'practical_experience', 'prof"
                "iciency', 'unspecified')"
            ),
            name="ck_qualification_level_code",
        ),
        sa.CheckConstraint(
            (
                "(start_offset IS NULL AND end_offset IS NULL) OR (start_offset IS NOT "
                "NULL AND end_offset IS NOT NULL AND start_offset >= 0 AND end_offset >"
                " start_offset)"
            ),
            name="ck_mention_offsets",
        ),
        sa.CheckConstraint("end_offset = CAST(end_offset AS BIGINT)", name="ck_end_offset_integer"),
        sa.CheckConstraint(
            "start_offset = CAST(start_offset AS BIGINT)", name="ck_start_offset_integer"
        ),
        sa.ForeignKeyConstraint(
            ["job_posting_id"],
            ["job_postings.id"],
            name=op.f("fk_keyword_mentions_job_posting_id_job_postings"),
        ),
        sa.ForeignKeyConstraint(
            ["keyword_id"], ["keywords.id"], name=op.f("fk_keyword_mentions_keyword_id_keywords")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_keyword_mentions")),
    )
    op.create_index(
        "ix_mentions_keyword_context", "keyword_mentions", ["keyword_id", "context"], unique=False
    )
    op.create_index("ix_mentions_posting", "keyword_mentions", ["job_posting_id"], unique=False)
    op.create_table(
        "review_issues",
        sa.Column("message_revision_id", _ID, nullable=False),
        sa.Column("extraction_run_id", _ID, nullable=False),
        sa.Column("job_posting_id", _ID, nullable=True),
        sa.Column("field_path", sa.String(length=120), nullable=False),
        sa.Column("issue_code", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("impact", sa.String(), nullable=False),
        sa.Column("affected_metric", sa.String(), nullable=True),
        sa.Column("status", sa.String(), server_default="open", nullable=False),
        sa.Column("created_at", _UTC_TIME, nullable=False),
        sa.Column("resolved_at", _UTC_TIME, nullable=True),
        sa.Column("resolution_run_id", _ID, nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("id", _ID, autoincrement="ignore_fk", nullable=False),
        sa.CheckConstraint(
            (
                "(impact = 'blocks_metric' AND affected_metric IS NOT NULL) OR (impact "
                "<> 'blocks_metric' AND affected_metric IS NULL)"
            ),
            name="ck_issue_metric_impact",
        ),
        sa.CheckConstraint(
            (
                "affected_metric IN ('role', 'keywords', 'seniority', 'experience', 'co"
                "mpensation', 'salary', 'work_arrangement')"
            ),
            name="ck_affected_metric_code",
        ),
        sa.CheckConstraint(
            "impact IN ('blocks_posting', 'blocks_metric', 'informational')", name="ck_impact_code"
        ),
        sa.CheckConstraint("status IN ('open', 'resolved', 'dismissed')", name="ck_status_code"),
        sa.ForeignKeyConstraint(
            ["extraction_run_id", "job_posting_id"],
            ["job_postings.extraction_run_id", "job_postings.id"],
            name="fk_issues_posting_run",
        ),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_issues")),
    )
    op.create_index(
        "ix_issues_resolution_run", "review_issues", ["resolution_run_id"], unique=False
    )
    op.create_index(
        "ix_issues_run_status", "review_issues", ["extraction_run_id", "status"], unique=False
    )
    op.create_index("ix_issues_status", "review_issues", ["status"], unique=False)
    op.create_table(
        "salary_offers",
        sa.Column("job_posting_id", _ID, nullable=False),
        sa.Column("minimum_amount_scaled", sa.BigInteger(), nullable=True),
        sa.Column("maximum_amount_scaled", sa.BigInteger(), nullable=True),
        sa.Column("bound_type", sa.String(), nullable=False),
        sa.Column("currency_symbol", sa.String(), nullable=True),
        sa.Column("currency_code", sa.String(length=3), nullable=True),
        sa.Column("payment_period", sa.String(), nullable=False),
        sa.Column("tax_basis", sa.String(), nullable=False),
        sa.Column("compensation_component", sa.String(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("field_states", sa.JSON(), nullable=False),
        sa.Column("id", _ID, autoincrement="ignore_fk", nullable=False),
        sa.CheckConstraint(
            (
                "(bound_type = 'exact' AND minimum_amount_scaled IS NOT NULL AND maximu"
                "m_amount_scaled IS NOT NULL AND minimum_amount_scaled = maximum_amount"
                "_scaled) OR (bound_type = 'range' AND minimum_amount_scaled IS NOT NUL"
                "L AND maximum_amount_scaled IS NOT NULL AND minimum_amount_scaled <= m"
                "aximum_amount_scaled) OR (bound_type = 'lower_only' AND minimum_amount"
                "_scaled IS NOT NULL AND maximum_amount_scaled IS NULL) OR (bound_type "
                "= 'upper_only' AND minimum_amount_scaled IS NULL AND maximum_amount_sc"
                "aled IS NOT NULL)"
            ),
            name="ck_salary_bound_shape",
        ),
        sa.CheckConstraint(
            "bound_type IN ('exact', 'range', 'lower_only', 'upper_only')",
            name="ck_bound_type_code",
        ),
        sa.CheckConstraint(
            "compensation_component IN ('base', 'bonus', 'total', 'unspecified')",
            name="ck_compensation_component_code",
        ),
        sa.CheckConstraint(
            (
                "length(currency_code) = 3 AND substr(currency_code, 1, 1) IN ('A', 'B'"
                ", 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M', 'N', 'O', 'P'"
                ", 'Q', 'R', 'S', 'T', 'U', 'V', 'W', 'X', 'Y', 'Z') AND substr(currenc"
                "y_code, 2, 1) IN ('A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K"
                "', 'L', 'M', 'N', 'O', 'P', 'Q', 'R', 'S', 'T', 'U', 'V', 'W', 'X', 'Y"
                "', 'Z') AND substr(currency_code, 3, 1) IN ('A', 'B', 'C', 'D', 'E', '"
                "F', 'G', 'H', 'I', 'J', 'K', 'L', 'M', 'N', 'O', 'P', 'Q', 'R', 'S', '"
                "T', 'U', 'V', 'W', 'X', 'Y', 'Z')"
            ),
            name="ck_currency_code_ascii",
        ),
        sa.CheckConstraint(
            ("payment_period IN ('hour', 'day', 'week', 'month', 'year', 'unspecified')"),
            name="ck_payment_period_code",
        ),
        sa.CheckConstraint(
            "tax_basis IN ('gross', 'net', 'unspecified')", name="ck_tax_basis_code"
        ),
        sa.CheckConstraint(
            "maximum_amount_scaled = CAST(maximum_amount_scaled AS BIGINT)",
            name="ck_maximum_amount_scaled_integer",
        ),
        sa.CheckConstraint(
            "maximum_amount_scaled >= 0", name="ck_maximum_amount_scaled_nonnegative"
        ),
        sa.CheckConstraint(
            "minimum_amount_scaled = CAST(minimum_amount_scaled AS BIGINT)",
            name="ck_minimum_amount_scaled_integer",
        ),
        sa.CheckConstraint(
            "minimum_amount_scaled >= 0", name="ck_minimum_amount_scaled_nonnegative"
        ),
        sa.ForeignKeyConstraint(
            ["job_posting_id"],
            ["job_postings.id"],
            name=op.f("fk_salary_offers_job_posting_id_job_postings"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_salary_offers")),
    )
    op.create_index("ix_salary_posting", "salary_offers", ["job_posting_id"], unique=False)

    if not sqlite:
        op.create_foreign_key(*_CURRENT_REVISION)
        op.create_foreign_key(*_ACTIVE_RUN)


def downgrade() -> None:
    # Break the optional cycles before dropping tables; keep SQLite FKs on.
    if op.get_context().dialect.name == "sqlite":
        op.execute("UPDATE messages SET current_revision_id = NULL")
        op.execute("UPDATE message_revisions SET active_extraction_run_id = NULL")
    else:
        op.drop_constraint(_CURRENT_REVISION[0], "messages", type_="foreignkey")
        op.drop_constraint(_ACTIVE_RUN[0], "message_revisions", type_="foreignkey")
    for table in (
        "review_issues",
        "keyword_mentions",
        "experience_requirements",
        "salary_offers",
        "duplicate_links",
        "job_postings",
        "extraction_runs",
        "message_revisions",
        "messages",
        "keywords",
        "sources",
    ):
        op.drop_table(table)
