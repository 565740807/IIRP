"""Index the newest complete SEC latest-feed scan that planning looks up.

Until a latest scan first completes no row qualifies, so without this partial
index the lookup scanned the whole job table on every planning pass and hit
the statement timeout on the live database.
"""
from alembic import op
from sqlalchemy import text

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

NAME = "ix_job_sec_latest_complete"
# Must stay identical to iirp.models.SEC_LATEST_COMPLETE so the planner can use it;
# SQLAlchemy renders checkpoint["sec_scan"] as a jsonb subscript, not "->".
PREDICATE = (
    "kind = 'sec_discover' AND status = 'SUCCEEDED' AND (target ->> 'mode') = 'latest' "
    "AND (checkpoint['sec_scan'] ->> 'complete') = 'true'"
)


def _index():
    # The job table is large and written continuously: build without blocking.
    with op.get_context().autocommit_block():
        connection = op.get_bind()
        previous = {key: connection.scalar(text(f"SHOW {key}"))
                    for key in ("statement_timeout", "lock_timeout")}
        try:
            op.execute("SET statement_timeout = '600s'")
            op.execute("SET lock_timeout = '3s'")
            valid = connection.scalar(text(
                "SELECT i.indisvalid FROM pg_index i WHERE i.indexrelid = to_regclass(:name)"),
                {"name": NAME})
            if valid is False:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {NAME}")
            if valid is not True:
                op.execute(f"CREATE INDEX CONCURRENTLY {NAME} ON job (created_at DESC) WHERE {PREDICATE}")
        finally:
            for key, value in previous.items():
                connection.execute(text("SELECT set_config(:key, :value, false)"),
                                   {"key": key, "value": value})


def upgrade():
    _index()


def downgrade():
    # Inside the migration transaction: a failed multi-step downgrade rolls back whole.
    op.execute(f"DROP INDEX IF EXISTS {NAME}")
