"""Reduce compression cost when publishing large frozen research results."""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    # Metadata only: existing JSONB/TOAST values retain their bytes and codec.
    # The supported PostgreSQL image includes LZ4. Do not rewrite old results.
    op.execute("ALTER TABLE analysis_result ALTER COLUMN data SET COMPRESSION lz4")


def downgrade():
    # Restores the previous future-write policy; LZ4 values remain readable.
    op.execute("ALTER TABLE analysis_result ALTER COLUMN data SET COMPRESSION default")
