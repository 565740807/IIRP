"""Daily prices become a 24-hour cache (D14).

``price_cache`` keeps one provider response per security (one request for the
whole needed range plus a month of buffer) and ``price_cache_bar`` its bars.
Research results carry the earliest expiry of the caches they used and are
deleted with them. The long-term maintained set (``security.maintain`` and
``active_until``) and the daily/weekly price update schedule are removed.

Only structure and small tables change here. Results computed from the old
versioned prices are marked expired; the worker deletes them like any other
expired result. The old versioned price tables (``price_dataset_version``,
``market_bar_revision``, ``dataset_bar``, ``corporate_action``) are no longer
read or written; dropping them and their data is a separate, confirmed step.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.create_table(
        "price_cache",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("security_id", sa.String(36), sa.ForeignKey("security.id"), nullable=False,
                  unique=True),
        sa.Column("start_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date, nullable=False),
        sa.Column("complete_through", sa.Date, nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("details", JSONB, nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_price_cache_expires_at", "price_cache", ["expires_at"])
    numeric = sa.Numeric(30, 12)
    op.create_table(
        "price_cache_bar",
        sa.Column("cache_id", sa.String(36),
                  sa.ForeignKey("price_cache.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("session_date", sa.Date, primary_key=True),
        sa.Column("open", numeric),
        sa.Column("high", numeric),
        sa.Column("low", numeric),
        sa.Column("close", numeric),
        sa.Column("adj_close", numeric),
        sa.Column("volume", sa.Numeric(32, 4)),
        sa.Column("dividends", numeric),
        sa.Column("splits", numeric),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text),
    )
    # Results computed from the old versioned prices lapse now. A non-volatile
    # default is stored in the catalog, so existing rows (and their compressed
    # payloads) are not rewritten; new rows always set their own expiry.
    op.add_column("analysis_result", sa.Column("expires_at", sa.DateTime(timezone=True),
                                               server_default=sa.text("now()")))
    op.alter_column("analysis_result", "expires_at", server_default=None)
    op.create_index("ix_analysis_result_expires_at", "analysis_result", ["expires_at"])
    op.drop_column("security", "maintain")
    op.drop_column("security", "active_until")
    # Home quotes keep their own short cache; there is no scheduled price update.
    op.execute(
        "UPDATE collection_strategy SET next_run_at = NULL, "
        "options = options - 'market_session' - 'market_revision_week' - 'revision_cursor' "
        "WHERE key IN ('market', 'earnings')"
    )


def downgrade():
    # Structure only: cached prices are refetchable and the old maintained set
    # comes back empty.
    op.add_column("security", sa.Column("active_until", sa.DateTime(timezone=True)))
    op.add_column("security", sa.Column("maintain", sa.Boolean, nullable=False,
                                        server_default=sa.false()))
    op.drop_index("ix_analysis_result_expires_at", table_name="analysis_result")
    op.drop_column("analysis_result", "expires_at")
    op.drop_table("price_cache_bar")
    op.drop_index("ix_price_cache_expires_at", table_name="price_cache")
    op.drop_table("price_cache")
