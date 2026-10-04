"""Persist request aliases across paused reuse and restart."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "request_receipt",
        sa.Column("request_id", sa.String(128), primary_key=True),
        sa.Column("batch_id", sa.String(36), sa.ForeignKey("batch.id"), nullable=False),
        sa.Column("scope_key", sa.String(64), nullable=False),
    )
    op.execute(
        "INSERT INTO request_receipt (request_id,batch_id,scope_key) SELECT request_id,id,scope_key FROM batch"
    )


def downgrade():
    op.drop_table("request_receipt")
