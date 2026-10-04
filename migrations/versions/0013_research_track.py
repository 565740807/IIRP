"""Independent latest-research pointers without rewriting saved snapshots."""
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("research_track",
        sa.Column("origin_id", sa.String(36), sa.ForeignKey("analysis_request.id"), primary_key=True),
        sa.Column("latest_id", sa.String(36), sa.ForeignKey("analysis_request.id"), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_research_track_latest_id", "research_track", ["latest_id"])


def downgrade():
    op.drop_table("research_track")
