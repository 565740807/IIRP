"""Keep each index member's sub-industry and the classification scheme it belongs to.

The S&P 500 list carries GICS sectors and sub-industries; the Nasdaq-100 table on
Wikipedia carries ICB industries and subsectors, stored as given. Existing rows get
both on the next weekly update.
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_index_sub_industry"
down_revision = "0004_overview_covering_index"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("index_constituent", sa.Column("sub_industry", sa.Text(), nullable=True))
    op.add_column("index_constituent", sa.Column("classification", sa.String(length=8), nullable=True))


def downgrade():
    op.drop_column("index_constituent", "classification")
    op.drop_column("index_constituent", "sub_industry")
