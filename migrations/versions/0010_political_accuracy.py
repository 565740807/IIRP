"""Keep bond issuer links separate from reported equity symbols."""
import sqlalchemy as sa
from alembic import op

revision = "0010_political_accuracy"
down_revision = "0009_political_disclosures"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("political_transaction", sa.Column("issuer_ticker", sa.String(24), nullable=True))
    op.add_column("political_transaction", sa.Column("issuer_ticker_origin", sa.String(16), nullable=True))


def downgrade():
    op.drop_column("political_transaction", "issuer_ticker_origin")
    op.drop_column("political_transaction", "issuer_ticker")
