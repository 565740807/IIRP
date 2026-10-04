"""Persist fair planning turns separately from user-visible activity timestamps."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("batch", sa.Column("last_planned_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_batch_last_planned_at", "batch", ["last_planned_at"])


def downgrade():
    op.drop_index("ix_batch_last_planned_at", table_name="batch")
    op.drop_column("batch", "last_planned_at")
