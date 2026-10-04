"""Let recent company/person history stop after the requested ordered rows."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    for basis in ("transaction_date", "accepted_at"):
        op.create_index(
            f"ix_event_recent_{basis}",
            "transaction_event",
            [sa.text(f"{basis} DESC NULLS LAST"), sa.text("id DESC")],
        )


def downgrade():
    for basis in ("transaction_date", "accepted_at"):
        op.drop_index(f"ix_event_recent_{basis}", table_name="transaction_event")
