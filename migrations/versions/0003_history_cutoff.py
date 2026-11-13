"""Persist the initial channel head for restart-safe backfill."""

import sqlalchemy as sa
from alembic import op

revision = "0003_history_cutoff"
down_revision = "0002_extraction_retry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column("history_upper_message_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sources", "history_upper_message_id")
