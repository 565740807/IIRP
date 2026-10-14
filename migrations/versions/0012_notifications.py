"""Keep notifications about followed people and rule matches, and the messages pushed to a phone."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012_notifications"
down_revision = "0011_notable_people"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "notification",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("dedupe_key", sa.Text(), nullable=False, unique=True),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("person_key", sa.Text(), nullable=True),
        sa.Column("issuer_id", sa.String(length=10), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("push_state", sa.String(length=16), nullable=True),
    )
    op.create_index("ix_notification_created", "notification", ["created_at"])
    op.create_index("ix_notification_push_state", "notification", ["push_state"])
    op.create_table(
        "notification_push",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_notification_push_status", "notification_push", ["status"])
    op.create_index("ix_notification_push_sent_at", "notification_push", ["sent_at"])


def downgrade():
    op.drop_table("notification_push")
    op.drop_table("notification")
