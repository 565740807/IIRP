"""SEC latest polling keeps one state row per source; refetchable sources expire.

Before this, every latest-feed poll created a permanent ``sec_discover`` job and
its continuation chain, plus a permanent list page and response JSON. Polling
state now lives in ``source_poll``; list pages, index files and discovery
response JSON written from now on carry ``source_object.expires_at`` (7 days).
Existing rows keep ``expires_at`` NULL: history cleanup is a separate step.

Unfinished latest-feed jobs from the old scheme are cancelled (not deleted).
The watermark starts at the newest accepted filing already saved: the old
chains walked the whole feed, so the first poll can close continuity there.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

INDEX = "ix_source_object_expires_at"
ACTIVE = "('QUEUED','RUNNING','PAUSE_REQUESTED','PAUSED','CANCEL_REQUESTED','RETRY_WAIT')"


def upgrade():
    # Data steps below touch the large job/batch_job tables on a rotational disk.
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.create_table(
        "source_poll",
        sa.Column("source", sa.String(32), primary_key=True),
        sa.Column("watermark_at", sa.DateTime(timezone=True)),
        sa.Column("catchup", JSONB),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_polled_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_complete_at", sa.DateTime(timezone=True)),
        sa.Column("failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text),
        sa.Column("last_result", JSONB),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute(
        "INSERT INTO source_poll (source, watermark_at, next_poll_at, updated_at) "
        "SELECT 'sec_latest', (SELECT max(accepted_at) FROM filing WHERE accepted_at <= now()), now(), now()"
    )
    # Adding a nullable column without a default only changes the catalog.
    op.add_column("source_object", sa.Column("expires_at", sa.DateTime(timezone=True)))
    op.execute(
        "UPDATE job SET status='CANCELLED', requested_action=NULL, lease_token=NULL, lease_until=NULL, "
        "finished_at=now(), updated_at=now(), error='最新申报改为按来源轮询，不再使用逐次任务' "
        f"WHERE kind='sec_discover' AND (target ->> 'mode')='latest' AND status IN {ACTIVE}"
    )
    # Open latest demands stop subscribing to those jobs, so planning no longer
    # loads thousands of obsolete discovery links on every pass.
    op.execute(
        "UPDATE batch_job bj SET active=false FROM job j, request_scope rs, batch b "
        "WHERE bj.job_id=j.id AND bj.scope_id=rs.id AND rs.batch_id=b.id AND bj.active "
        "AND j.kind='sec_discover' AND (j.target ->> 'mode')='latest' AND b.kind='sec_latest' "
        f"AND b.status IN {ACTIVE}"
    )
    with op.get_context().autocommit_block():
        connection = op.get_bind()
        previous = {key: connection.scalar(text(f"SHOW {key}"))
                    for key in ("statement_timeout", "lock_timeout")}
        try:
            op.execute("SET statement_timeout = '600s'")
            op.execute("SET lock_timeout = '3s'")
            valid = connection.scalar(text(
                "SELECT i.indisvalid FROM pg_index i WHERE i.indexrelid = to_regclass(:name)"),
                {"name": INDEX})
            if valid is False:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
            if valid is not True:
                op.execute(f"CREATE INDEX CONCURRENTLY {INDEX} ON source_object (expires_at) "
                           "WHERE expires_at IS NOT NULL")
        finally:
            for key, value in previous.items():
                connection.execute(text("SELECT set_config(:key, :value, false)"),
                                   {"key": key, "value": value})


def downgrade():
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.drop_column("source_object", "expires_at")
    op.drop_table("source_poll")
