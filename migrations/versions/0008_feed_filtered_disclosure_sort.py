"""Use the displayed filtered disclosure time before freezing feed pagination."""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""UPDATE feed_group_revision f SET transaction_sort_dates = transaction_sort_dates || (
        SELECT coalesce(jsonb_object_agg('accepted:' || kind, sort_date), '{}'::jsonb) FROM (
            SELECT kind, max((CASE WHEN r->>'is_amendment_update'='true'
                THEN r->>'accepted_at'
                ELSE coalesce(t.data->>'original_accepted_at', r->>'group_accepted_at', r->>'accepted_at')
                END)::timestamptz) AS sort_date
            FROM jsonb_array_elements(f.data->'transactions') r
            LEFT JOIN transaction_event t ON t.id=r->>'id'
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
    op.execute("""UPDATE feed_group_revision SET transaction_sort_dates = transaction_sort_dates
        - ARRAY['accepted:all','accepted:focus','accepted:buy','accepted:sell','accepted:derivative','accepted:other']""")
