"""Bound historical job lookups, pending feed probes, and manifest GC scans."""
from alembic import op
from sqlalchemy import text

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

INDEXES = {
    "ix_job_history_key": "ON job (idempotency_key, created_at DESC)",
    "ix_job_sec_document_accession_latest": (
        "ON job ((target ->> 'accession'), created_at DESC, id DESC) "
        "WHERE kind = 'sec_document'"
    ),
    "ix_filing_pending": "ON filing (accession) WHERE visible IS TRUE AND current_version IS NULL",
    "ix_feed_manifest_created": "ON feed_manifest (created_at, sha256)",
}


def _indexes(*, remove=False):
    # Concurrent DDL cannot run inside a transaction. Earlier migration work is
    # committed by this block; failed builds can leave invalid indexes, so reruns
    # replace only those artifacts and preserve already-valid indexes.
    with op.get_context().autocommit_block():
        connection = op.get_bind()
        previous = {key: connection.scalar(text(f"SHOW {key}"))
                    for key in ("statement_timeout", "lock_timeout")}
        try:
            op.execute("SET statement_timeout = '180s'")
            op.execute("SET lock_timeout = '3s'")
            for name, definition in INDEXES.items():
                valid = connection.scalar(text(
                    "SELECT i.indisvalid FROM pg_index i "
                    "WHERE i.indexrelid = to_regclass(:name)"), {"name": name})
                if remove or valid is False:
                    op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
                if not remove and valid is not True:
                    op.execute(f"CREATE INDEX CONCURRENTLY {name} {definition}")
        finally:
            for key, value in previous.items():
                connection.execute(text("SELECT set_config(:key, :value, false)"),
                                   {"key": key, "value": value})


def upgrade():
    _indexes()


def downgrade():
    _indexes(remove=True)
