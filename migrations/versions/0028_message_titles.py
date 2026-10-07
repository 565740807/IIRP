"""Titles hold message codes (S4).

Generated job, batch and event-set titles are now encoded messages
(``iirp.messages``) instead of finished Chinese sentences, and a message with
its parameters can be longer than the old 160/200-character limits. Changing
``varchar(n)`` to ``text`` needs no table rewrite in PostgreSQL. User-typed
titles stay limited to 200 characters by the API.
"""
import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

COLUMNS = (("job", 160), ("batch", 200), ("event_set", 200))


def upgrade():
    for table, _ in COLUMNS:
        op.alter_column(table, "title", type_=sa.Text())


def downgrade():
    for table, length in COLUMNS:
        op.alter_column(table, "title", type_=sa.String(length), postgresql_using=f"left(title, {length})")
