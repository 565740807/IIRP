"""Index active demand lookups by job; preserve all subscriptions and priority rules."""
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_batch_job_job_active", "batch_job", ["job_id", "active"])


def downgrade():
    op.drop_index("ix_batch_job_job_active", table_name="batch_job")
