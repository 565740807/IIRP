"""Keep the user's watched stock codes."""

import sqlalchemy as sa
from alembic import op

revision = "0007_watchlist"
down_revision = "0006_long_prices"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "watchlist_item",
        sa.Column("ticker", sa.String(length=24), primary_key=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("watchlist_item")
