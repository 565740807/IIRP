"""Schema baseline: every table, index and constraint of the initial release.

Later schema changes are ordinary incremental migrations on top of this one.
"""

from pathlib import Path

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = Path(__file__).with_name("0001_baseline.sql")


def statements():
    """pg_dump output: one statement per block ending in ';' (there are no function bodies)."""
    current = []
    for line in SCHEMA.read_text().splitlines():
        if line.startswith("--") or not (line.strip() or current):
            continue
        current.append(line)
        if line.rstrip().endswith(";"):
            yield "\n".join(current)
            current = []
    assert not current, "unterminated statement in the baseline schema"


def upgrade():
    connection = op.get_bind()
    for statement in statements():
        connection.exec_driver_sql(statement)


def downgrade():
    raise NotImplementedError("The baseline is the first revision; drop the database instead.")
