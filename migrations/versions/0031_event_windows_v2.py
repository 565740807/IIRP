"""Earnings and event results gain benchmarks, paths and intervals; old results expire (S6b).

Results of the previous shape (event-windows-v1) are marked expired now; the
worker deletes them like any expired result, and an opened analysis fetches
again with the same frozen events. Saved event sets and prompt templates are
not touched.
"""
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.execute("""
        UPDATE analysis_result res SET expires_at = now()
        FROM analysis_request req
        WHERE req.id = res.analysis_id
          AND req.params ->> 'kind' = 'event_dates'
          AND (res.expires_at IS NULL OR res.expires_at > now())
    """)


def downgrade():
    # Cached results; nothing to restore.
    pass
