"""Reviewed event imports and immutable event-set versions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "event_set",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("security_id", sa.String(36), sa.ForeignKey("security.id"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_event_set_security_id", "event_set", ["security_id"])
    op.create_table(
        "event_import_preview",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("document", postgresql.JSONB(), nullable=False),
        sa.Column("warnings", postgresql.JSONB(), nullable=False),
        sa.Column("set_id", sa.String(36), sa.ForeignKey("event_set.id")),
        sa.Column("expected_version", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_event_import_preview_content_hash", "event_import_preview", ["content_hash"]
    )
    op.create_table(
        "event_set_version",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("set_id", sa.String(36), sa.ForeignKey("event_set.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "preview_id", sa.String(36), sa.ForeignKey("event_import_preview.id"), nullable=False
        ),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("document", postgresql.JSONB(), nullable=False),
        sa.Column("events", postgresql.JSONB(), nullable=False),
        sa.Column("reviews", postgresql.JSONB(), nullable=False),
        sa.Column("revision_note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("set_id", "version"),
    )
    op.create_index("ix_event_set_version_set_id", "event_set_version", ["set_id"])
    op.create_table(
        "event_command_receipt",
        sa.Column("request_id", sa.String(128), primary_key=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("response", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("event_command_receipt")
    op.drop_index("ix_event_set_version_set_id", table_name="event_set_version")
    op.drop_table("event_set_version")
    op.drop_index("ix_event_import_preview_content_hash", table_name="event_import_preview")
    op.drop_table("event_import_preview")
    op.drop_index("ix_event_set_security_id", table_name="event_set")
    op.drop_table("event_set")
