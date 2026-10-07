"""Issuer ticker for lookup (S5b).

The Insider lookup finds a company by the ticker its filings state. Reading
that from every filing version's JSON takes seconds, so the issuer keeps the
ticker of its most recently accepted visible filing (placeholders such as
"NONE" are empty), maintained when a filing is parsed.
"""
import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("issuer", sa.Column("ticker", sa.String(32), nullable=True))
    op.execute("""
        UPDATE issuer SET ticker = latest.ticker
        FROM (
            SELECT DISTINCT ON (f.issuer_id) f.issuer_id,
                   NULLIF(upper(btrim(v.data->>'issuer_ticker')), '') AS ticker
            FROM filing f JOIN filing_version v ON v.id = f.current_version
            WHERE f.visible AND f.issuer_id IS NOT NULL
            ORDER BY f.issuer_id, f.accepted_at DESC NULLS LAST, f.accession DESC
        ) latest
        WHERE issuer.id = latest.issuer_id
          AND latest.ticker NOT IN ('NONE', 'N/A', 'NA', 'NULL', 'UNKNOWN', 'NOT APPLICABLE', '--', '-')
    """)
    op.create_index("ix_issuer_ticker", "issuer", ["ticker"])


def downgrade():
    op.drop_index("ix_issuer_ticker", table_name="issuer")
    op.drop_column("issuer", "ticker")
