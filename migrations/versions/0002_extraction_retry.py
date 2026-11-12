"""Persist the next eligible time of a delayed extraction."""

import sqlalchemy as sa
from alembic import op

revision = "0002_extraction_retry"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    timestamp = sa.DateTime(timezone=True).with_variant(sa.DateTime(), "sqlite")
    op.add_column("extraction_runs", sa.Column("retry_not_before", timestamp, nullable=True))


def downgrade() -> None:
    op.drop_column("extraction_runs", "retry_not_before")
