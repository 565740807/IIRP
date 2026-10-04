"""Persist isolated batch planning failures and retry deadlines."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    # Metadata-only defaults on supported PostgreSQL; fail promptly if writers
    # prevent the short ACCESS EXCLUSIVE lock. Retry migration after contention.
    op.execute("SET LOCAL lock_timeout = '500ms'")
    op.add_column("batch", sa.Column("planning_failures", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("batch", sa.Column("planning_error", JSONB(), nullable=True))
    op.add_column("batch", sa.Column("planning_retry_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.execute("SET LOCAL lock_timeout = '500ms'")
    op.drop_column("batch", "planning_retry_at")
    op.drop_column("batch", "planning_error")
    op.drop_column("batch", "planning_failures")
