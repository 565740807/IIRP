"""Filter immutable feed revisions without loading every transaction payload."""

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "feed_group_revision",
        sa.Column("match_kinds", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "feed_group_revision",
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
    )
    # Keep old immutable payloads and session references; project only frozen filter semantics.
    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id, data->'transactions' AS transactions FROM feed_group_revision")
    )
    for row in rows:
        transactions = row.transactions or []
        kinds = {"all"} if transactions else set()
        for transaction in transactions:
            table, code = transaction.get("table"), transaction.get("code")
            if table == "II" or code in {"P", "S"}:
                kinds.add("focus")
            if table == "I" and code == "P":
                kinds.add("buy")
            if table == "I" and code == "S":
                kinds.add("sell")
            if table == "II":
                kinds.add("derivative")
            if table != "II" and code not in {"P", "S"}:
                kinds.add("other")
        connection.execute(
            sa.text(
                "UPDATE feed_group_revision SET match_kinds=CAST(:kinds AS jsonb), "
                "row_count=:count WHERE id=:id"
            ),
            {"id": row.id, "kinds": json.dumps(sorted(kinds)), "count": len(transactions)},
        )
    op.alter_column("feed_group_revision", "match_kinds", server_default=None)
    op.alter_column("feed_group_revision", "row_count", server_default=None)


def downgrade():
    op.drop_column("feed_group_revision", "row_count")
    op.drop_column("feed_group_revision", "match_kinds")
