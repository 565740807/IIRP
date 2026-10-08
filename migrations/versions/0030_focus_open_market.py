"""Key trades are open-market purchases and sales only; old research results expire (S6a).

1. The feed's "focus" filter used to include every derivative row (grants,
   exercises). It now means table I purchases (P) and sales (S), which is
   exactly the union of the "buy" and "sell" filters. Each revision's stored
   facet list and focus sort dates are re-derived from its buy/sell ones, and
   the focus sort keys of current groups are rewritten. Reported rows are not
   touched.
2. Monthly and interval results switch to the open → close basis (D6). Old
   results (previous-close basis, old shape) are marked expired now; the worker
   deletes them like any expired result and an opened research refetches.
"""
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

MISSING = "0001-01-01 00:00:00+00"


def upgrade():
    op.execute("SET LOCAL statement_timeout = '900s'")
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.execute("""
        UPDATE feed_group_revision r SET
            match_kinds = COALESCE((
                SELECT jsonb_agg(v.kind ORDER BY v.position)
                FROM (VALUES ('all', 1), ('focus', 2), ('buy', 3), ('sell', 4),
                             ('derivative', 5), ('other', 6)) AS v(kind, position)
                WHERE CASE WHEN v.kind = 'focus'
                           THEN r.match_kinds ?| array['buy', 'sell']
                           ELSE r.match_kinds ? v.kind END
            ), '[]'::jsonb),
            transaction_sort_dates = (COALESCE(r.transaction_sort_dates, '{}'::jsonb)
                                      - 'focus' - 'accepted\\:focus')
                || CASE WHEN r.match_kinds ?| array['buy', 'sell'] THEN jsonb_build_object(
                    'focus', GREATEST(COALESCE(r.transaction_sort_dates ->> 'buy', ''),
                                      COALESCE(r.transaction_sort_dates ->> 'sell', '')),
                    'accepted\\:focus', GREATEST(COALESCE(r.transaction_sort_dates ->> 'accepted\\:buy', ''),
                                               COALESCE(r.transaction_sort_dates ->> 'accepted\\:sell', '')))
                   ELSE '{}'::jsonb END
        WHERE r.match_kinds ?| array['focus', 'buy', 'sell']
    """)
    op.execute("DELETE FROM feed_group_order WHERE kind = 'focus'")
    op.execute(f"""
        INSERT INTO feed_group_order
            (kind, sort_order, group_key, sort_key, accepted_at, revision_id, seq, xid)
        SELECT 'focus', o.sort_order, c.group_key,
               CASE o.sort_order
                 WHEN 'transaction' THEN COALESCE(
                   (NULLIF(r.transaction_sort_dates ->> 'focus', '')::date)::timestamp AT TIME ZONE 'UTC',
                   '{MISSING}'::timestamptz)
                 ELSE COALESCE(
                   NULLIF(r.transaction_sort_dates ->> 'accepted\\:focus', '')::timestamptz,
                   '{MISSING}'::timestamptz)
               END,
               r.accepted_at, r.id, r.seq, r.xid
        FROM feed_group_current c
        JOIN feed_group_revision r ON r.id = c.revision_id
        CROSS JOIN (VALUES ('transaction'), ('accepted')) AS o(sort_order)
        WHERE c.row_count > 0 AND r.match_kinds ? 'focus'
    """)
    op.execute("""
        UPDATE analysis_result res SET expires_at = now()
        FROM analysis_request req
        WHERE req.id = res.analysis_id
          AND req.params ->> 'kind' IN ('monthly', 'interval')
          AND (res.expires_at IS NULL OR res.expires_at > now())
    """)


def downgrade():
    # Derived feed metadata and cached results; nothing to restore.
    pass
