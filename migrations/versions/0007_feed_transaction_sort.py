"""Scalar per-filter dates permit stable transaction sorting before pagination."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("feed_group_revision", sa.Column("transaction_sort_dates", postgresql.JSONB(), nullable=False, server_default="{}"))
    op.execute("""UPDATE feed_group_revision f SET transaction_sort_dates = (
        SELECT coalesce(jsonb_object_agg(kind, sort_date), '{}'::jsonb) FROM (
            SELECT kind, max(r->>'transaction_date') AS sort_date
            FROM jsonb_array_elements(f.data->'transactions') r
            CROSS JOIN unnest(ARRAY['all','focus','buy','sell','derivative','other']) kind
            WHERE kind='all'
               OR (kind='focus' AND (r->>'table'='II' OR r->>'code' IN ('P','S')))
               OR (kind='buy' AND r->>'table'='I' AND r->>'code'='P')
               OR (kind='sell' AND r->>'table'='I' AND r->>'code'='S')
               OR (kind='derivative' AND r->>'table'='II')
               OR (kind='other' AND (r->>'table' IS NULL OR r->>'table'!='II') AND (r->>'code' IS NULL OR r->>'code' NOT IN ('P','S')))
            GROUP BY kind
        ) dates
    )""")


def downgrade():
    op.drop_column("feed_group_revision", "transaction_sort_dates")
