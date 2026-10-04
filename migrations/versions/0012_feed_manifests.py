"""Share new immutable feed indexes; preserve all existing inline reading sessions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("feed_manifest",
                    sa.Column("sha256", sa.String(64), primary_key=True),
                    sa.Column("revision_ids", postgresql.JSONB(), nullable=False),
                    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.add_column("feed_session", sa.Column("manifest_hash", sa.String(64), nullable=True))
    op.create_foreign_key("fk_feed_session_manifest_hash", "feed_session", "feed_manifest", ["manifest_hash"], ["sha256"])
    op.create_index("ix_feed_session_manifest_hash", "feed_session", ["manifest_hash"])
    op.create_index("ix_source_observation_source_hash", "source_observation", ["source_hash"])


def downgrade():
    # Restore the old representation before removing the shared-index schema.
    # Session identities, ordering, expiry and historical facts remain unchanged.
    op.execute("""UPDATE feed_session s SET revision_ids=m.revision_ids
        FROM feed_manifest m WHERE s.manifest_hash=m.sha256""")
    op.drop_index("ix_source_observation_source_hash", table_name="source_observation")
    op.drop_index("ix_feed_session_manifest_hash", table_name="feed_session")
    op.drop_constraint("fk_feed_session_manifest_hash", "feed_session", type_="foreignkey")
    op.drop_column("feed_session", "manifest_hash")
    op.drop_table("feed_manifest")
