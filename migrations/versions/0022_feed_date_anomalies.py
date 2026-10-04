"""Transactions dated after their own SEC acceptance date never lead feed sorting.

Stored sort dates of revisions containing such a row (e.g. a mistyped year)
are recomputed without it, and the sort keys of affected current groups are
rewritten. The reported rows and every other revision field stay unchanged.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from alembic import op
from sqlalchemy import text

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

ET = ZoneInfo("America/New_York")
MISSING = "0001-01-01 00:00:00+00"


# Frozen copies of the read-path rules in iirp.sec_facts at this revision.
def _matches(row, kind):
    table, code = row.get("table"), row.get("code")
    return {
        "all": True,
        "focus": table == "II" or code in {"P", "S"},
        "buy": table == "I" and code == "P",
        "sell": table == "I" and code == "S",
        "derivative": table == "II",
        "other": table != "II" and code not in {"P", "S"},
    }[kind]


def _sort_transaction_date(row):
    transaction, accepted = row.get("transaction_date"), row.get("accepted_at")
    if transaction and accepted:
        day = datetime.fromisoformat(accepted).astimezone(ET).date().isoformat()
        if str(transaction) > day:
            return ""
    return transaction or ""


def _sort_dates(rows, facets):
    return {
        **{kind: max((_sort_transaction_date(row) for row in rows if _matches(row, kind)), default="")
           for kind in facets},
        **{"accepted:" + kind: max((row.get("group_accepted_at") or row.get("accepted_at") or ""
                                     for row in rows if _matches(row, kind)), default="")
           for kind in facets},
    }


def upgrade():
    connection = op.get_bind()
    op.execute("SET LOCAL statement_timeout = '900s'")
    op.execute("SET LOCAL lock_timeout = '3s'")
    # Every row of a group was accepted on the group's ET day, so a stored
    # transaction sort date after that day can only come from such a row.
    candidates = connection.execute(text("""
        SELECT r.id, r.match_kinds, r.data -> 'transactions' AS rows
        FROM feed_group_revision r
        WHERE EXISTS (
            SELECT 1 FROM jsonb_each_text(r.transaction_sort_dates) e
            WHERE e.key NOT LIKE 'accepted:%'
              AND e.value > to_char((r.accepted_at AT TIME ZONE 'America/New_York')::date, 'YYYY-MM-DD'))
    """)).all()
    for identifier, facets, rows in candidates:
        connection.execute(
            text("UPDATE feed_group_revision SET transaction_sort_dates = CAST(:dates AS jsonb) WHERE id = :id"),
            {"id": identifier, "dates": json.dumps(_sort_dates(rows or [], facets or []))},
        )
    ids = [row[0] for row in candidates]
    if not ids:
        return
    groups = [row[0] for row in connection.execute(
        text("SELECT group_key FROM feed_group_current WHERE revision_id = ANY(:ids) AND row_count > 0"),
        {"ids": ids})]
    if not groups:
        return
    connection.execute(text("DELETE FROM feed_group_order WHERE group_key = ANY(:groups)"),
                       {"groups": groups})
    connection.execute(text(f"""
        INSERT INTO feed_group_order
            (kind, sort_order, group_key, sort_key, accepted_at, revision_id, seq, xid)
        SELECT k.kind, o.sort_order, c.group_key,
               CASE o.sort_order
                 WHEN 'transaction' THEN COALESCE(
                   (NULLIF(r.transaction_sort_dates ->> k.kind, '')::date)::timestamp AT TIME ZONE 'UTC',
                   '{MISSING}'::timestamptz)
                 ELSE COALESCE(
                   NULLIF(r.transaction_sort_dates ->> ('accepted:' || k.kind), '')::timestamptz,
                   '{MISSING}'::timestamptz)
               END,
               r.accepted_at, r.id, r.seq, r.xid
        FROM feed_group_current c
        JOIN feed_group_revision r ON r.id = c.revision_id
        CROSS JOIN LATERAL jsonb_array_elements_text(r.match_kinds) AS k(kind)
        CROSS JOIN (VALUES ('transaction'), ('accepted')) AS o(sort_order)
        WHERE c.group_key = ANY(:groups) AND c.row_count > 0
    """), {"groups": groups})


def downgrade():
    # The recomputed sort dates are a correction of derived metadata; the
    # reported rows were never changed, so there is nothing to restore.
    pass
