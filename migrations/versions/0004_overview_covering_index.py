"""Cover the Insider overview with one partial index on transaction_event.

Two JSON fields the overview returns become stored generated columns, so the
index can include every column the overview reads. Expression statistics on the
JSON fields in the index predicate give the planner real row estimates.
"""

from alembic import op

revision = "0004_overview_covering_index"
down_revision = "0003_insider_overview"
branch_labels = None
depends_on = None

ROWS = ("status = 'CURRENT' AND transaction_code IN ('P', 'S') AND (data ->> 'table') = 'I' "
        "AND ((data ->> 'currency') IS NULL OR (data ->> 'currency') = 'USD')")
COLUMNS = ("id, issuer_id, accession, version_id, accepted_at, transaction_code, trade_shares, "
           "reported_price, price_range_low, price_range_high, is_plan, is_ceo, is_cfo, "
           "is_president, is_chair, is_director, is_ten_percent, owner_ids, filing_ticker, "
           "shares_after")


def upgrade():
    # Adding stored generated columns rewrites the table; it runs with web and
    # worker stopped during the deployment and is bounded by rehearsal.
    op.execute("SET LOCAL statement_timeout = '600s'")
    op.execute("ALTER TABLE transaction_event "
               "ADD COLUMN filing_ticker text GENERATED ALWAYS AS (data ->> 'ticker') STORED, "
               "ADD COLUMN shares_after text GENERATED ALWAYS AS (data ->> 'shares_after') STORED")
    op.execute("DROP INDEX ix_event_overview")
    op.execute(f"CREATE INDEX ix_event_overview ON transaction_event (transaction_date) "
               f"INCLUDE ({COLUMNS}) WHERE {ROWS}")
    op.execute("CREATE STATISTICS st_event_table ON (data ->> 'table') FROM transaction_event")
    op.execute("CREATE STATISTICS st_event_currency ON (data ->> 'currency') FROM transaction_event")
    # The table rewrite cleared the visibility map; index-only scans need it back.
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '600s'")
        op.execute("VACUUM (ANALYZE) transaction_event")
        op.execute("RESET statement_timeout")


def downgrade():
    op.execute("DROP STATISTICS st_event_currency")
    op.execute("DROP STATISTICS st_event_table")
    op.execute("DROP INDEX ix_event_overview")
    op.execute("CREATE INDEX ix_event_overview ON transaction_event "
               "(transaction_date, issuer_id, transaction_code) "
               "WHERE status = 'CURRENT' AND transaction_code IN ('P', 'S')")
    op.execute("ALTER TABLE transaction_event DROP COLUMN shares_after, DROP COLUMN filing_ticker")
