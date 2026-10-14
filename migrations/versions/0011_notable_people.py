"""Keep the followed people, and find a filer's transactions without reading every row."""
import sqlalchemy as sa
from alembic import op

revision = "0011_notable_people"
down_revision = "0010_political_accuracy"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "notable_person",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Building the index reads every transaction row; an autovacuum on the table yields to it.
    op.execute("SET LOCAL lock_timeout = '60s'")
    op.execute("SET LOCAL statement_timeout = '600s'")
    op.create_index("ix_event_owner_ids", "transaction_event", ["owner_ids"], postgresql_using="gin")


def downgrade():
    op.drop_index("ix_event_owner_ids", table_name="transaction_event")
    op.drop_table("notable_person")
