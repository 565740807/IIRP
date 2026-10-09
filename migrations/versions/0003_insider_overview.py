"""Queryable Insider facts and weekly unofficial index membership."""

from alembic import op
from iirp.insider.typed import typed_fields
from sqlalchemy import text

revision = "0003_insider_overview"
down_revision = "0002_event_titles"
branch_labels = None
depends_on = None

FACT_COLUMNS = {
    "transaction_code": "varchar(8)", "trade_direction": "varchar(8)",
    "trade_shares": "numeric(30,12)", "reported_price": "numeric(30,12)",
    "reported_amount": "numeric(60,24)", "ownership_type": "varchar(8)",
    **{name: "boolean NOT NULL DEFAULT false" for name in (
        "is_plan", "is_ceo", "is_cfo", "is_president", "is_chair", "is_director", "is_ten_percent")},
    "price_range_low": "numeric(30,12)", "price_range_high": "numeric(30,12)",
}


def backfill(connection):
    """Keyset batches keep memory bounded and preserve every original JSON value."""
    last = ""
    statement = text("UPDATE transaction_event SET " + ",".join(f"{name}=:{name}" for name in FACT_COLUMNS)
                     + " WHERE id=:id")
    while True:
        rows = connection.execute(text(
            "SELECT e.id,e.data,COALESCE((SELECT jsonb_agg(o.relationship) FROM filing_owner o "
            "WHERE o.version_id=e.version_id), '[]'::jsonb) AS relationships "
            "FROM transaction_event e WHERE e.id>:last ORDER BY e.id LIMIT 1000"), {"last": last}).all()
        if not rows:
            break
        connection.execute(statement, [{"id": row.id, **typed_fields(row.data, row.relationships)} for row in rows])
        last = rows[-1].id


def upgrade():
    # No web/worker writers during the authorized deployment; bounded by rehearsal.
    op.execute("SET LOCAL statement_timeout = '240s'")
    for name, kind in FACT_COLUMNS.items():
        op.execute(f"ALTER TABLE transaction_event ADD COLUMN {name} {kind}")
    backfill(op.get_bind())
    op.execute("CREATE INDEX ix_event_overview ON transaction_event "
               "(transaction_date,issuer_id,transaction_code) "
               "WHERE status = 'CURRENT' AND transaction_code IN ('P', 'S')")
    op.execute("CREATE TABLE index_constituent (index_name varchar(16) NOT NULL,ticker varchar(32) NOT NULL,"
               "cik varchar(10),name text NOT NULL,industry text,updated_at timestamptz NOT NULL,"
               "PRIMARY KEY(index_name,ticker))")
    op.execute("CREATE INDEX ix_index_constituent_cik ON index_constituent(cik)")
    op.execute("CREATE TABLE index_constituent_state (index_name varchar(16) PRIMARY KEY,"
               "updated_at timestamptz,attempted_at timestamptz,error jsonb)")


def downgrade():
    op.drop_table("index_constituent_state")
    op.drop_table("index_constituent")
    op.drop_index("ix_event_overview", table_name="transaction_event")
    for name in reversed(FACT_COLUMNS):
        op.drop_column("transaction_event", name)
