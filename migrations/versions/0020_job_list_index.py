"""Index the task list: newest jobs first, keyset paged by (created_at, id)."""
from alembic import op
from sqlalchemy import text

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

NAME = "ix_job_created_id"


def _index(*, remove=False):
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
            if remove or valid is False:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {NAME}")
            if not remove and valid is not True:
                op.execute(f"CREATE INDEX CONCURRENTLY {NAME} ON job (created_at DESC, id DESC)")
        finally:
            for key, value in previous.items():
                connection.execute(text("SELECT set_config(:key, :value, false)"),
                                   {"key": key, "value": value})


def upgrade():
    _index()


def downgrade():
    _index(remove=True)
