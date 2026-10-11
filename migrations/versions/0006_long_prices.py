"""Keep daily index-member prices and durable retention controls."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_long_prices"
down_revision = "0005_index_sub_industry"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("long_price_series",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False, unique=True),
        sa.Column("security_id", sa.String(36), sa.ForeignKey("security.id"), nullable=False, unique=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("removed_on", sa.Date()),
        sa.Column("retain_from", sa.Date(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("needs_full", sa.Boolean(), nullable=False),
        *[sa.Column(k, sa.Date()) for k in ("start_date", "end_date", "complete_through")],
        *[sa.Column(k, sa.DateTime(timezone=True)) for k in ("fetched_at", "attempted_at")],
        sa.Column("details", postgresql.JSONB(), nullable=False))
    op.create_table("long_price_bar",
        sa.Column("cache_id", sa.String(36), sa.ForeignKey("long_price_series.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("session_date", sa.Date(), primary_key=True),
        *[sa.Column(k, sa.Numeric(30, 12), nullable=False) for k in ("open", "high", "low", "close")],
        sa.Column("volume", sa.Numeric(32, 4)),
        *[sa.Column(k, sa.Numeric(30, 12)) for k in ("dividends", "splits")],
        sa.Column("status", sa.String(32), nullable=False), sa.Column("reason", sa.Text()))


def downgrade():
    op.drop_table("long_price_bar")
    op.drop_table("long_price_series")
