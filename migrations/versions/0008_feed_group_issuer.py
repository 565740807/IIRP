"""Find the feed groups of a few companies (a watchlist): their issuers by ticker, then their groups.

Issuer tickers already have an index for single-class tickers; the few that name several
classes get a small partial index, and feed group pointers an index by issuer.
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_feed_group_issuer"
down_revision = "0007_watchlist"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_feed_group_current_issuer", "feed_group_current", ["issuer_id"])
    op.create_index("ix_issuer_multi_class", "issuer", ["id"],
                    postgresql_where=sa.text("ticker ~ '[,;[:space:]]'"))


def downgrade():
    op.drop_index("ix_issuer_multi_class", table_name="issuer")
    op.drop_index("ix_feed_group_current_issuer", table_name="feed_group_current")
