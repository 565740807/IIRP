"""Durable dependency-completion notifications, including pre-upgrade backlog."""
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_job_kind_status_finished", "job", ["kind", "status", "finished_at"])
    op.create_table("batch_plan_signal",
        sa.Column("batch_id", sa.String(36), sa.ForeignKey("batch.id"), primary_key=True),
        sa.Column("token", sa.String(36), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_batch_plan_signal_updated_at", "batch_plan_signal", ["updated_at"])
    # Record reconciliation work only. Do not alter jobs, controls or historical outcomes.
    op.execute("""INSERT INTO batch_plan_signal (batch_id, token, updated_at)
        SELECT id, gen_random_uuid()::text, now() FROM batch
        WHERE status IN ('QUEUED','RUNNING','RETRY_WAIT','PAUSE_REQUESTED','CANCEL_REQUESTED')""")


def downgrade():
    op.drop_index("ix_job_kind_status_finished", table_name="job")
    op.drop_table("batch_plan_signal")
