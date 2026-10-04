"""Index recent SEC claim stamps without indexing unrelated historical jobs."""
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    # CONCURRENTLY keeps the live SEC writer available. Drop a possible invalid
    # artifact from an interrupted previous attempt before the retry.
    with op.get_context().autocommit_block():
        # App connections default to a 5s statement timeout. Concurrent build
        # needs a bounded allowance for two scans and older transactions.
        op.execute("SET statement_timeout = '180s'")
        op.execute("SET lock_timeout = '3s'")
        try:
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_job_sec_claim_stamp")
            op.execute("""
                CREATE INDEX CONCURRENTLY ix_job_sec_claim_stamp
                ON job (kind, ((checkpoint ->> '_queue_sec_claimed_at')) DESC)
                WHERE (checkpoint ->> '_queue_sec_claimed_at') IS NOT NULL
            """)
        finally:
            op.execute("SET statement_timeout = '5s'")
            op.execute("SET lock_timeout = '500ms'")



def downgrade():
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '180s'")
        op.execute("SET lock_timeout = '3s'")
        try:
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_job_sec_claim_stamp")
        finally:
            op.execute("SET statement_timeout = '5s'")
            op.execute("SET lock_timeout = '500ms'")
