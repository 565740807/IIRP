"""Key trades (focus) are open-market purchases and sales only; migration 0030.

Real PostgreSQL in a disposable iirp_v1_test_* database; all filings are synthetic.
"""

from datetime import date

from alembic import command
from alembic.config import Config
from iirp.config import ROOT
from iirp.db import engine, session
from iirp.insider.feed import feed
from iirp.insider.views import _matches
from iirp.models import FeedGroupOrder, FeedRevision
from sqlalchemy import select, text

from tests.insider.test_feed_watermarks import publish
from tests.sec.test_sec_facts import clean, isolated_database  # noqa: F401


def test_focus_is_table_one_purchases_and_sales():
    assert _matches({"table": "I", "code": "P"}, "focus") and _matches({"table": "I", "code": "S"}, "focus")
    # Grants, exercises and derivative rows stay under "all".
    for row in ({"table": "I", "code": "A"}, {"table": "I", "code": "M"},
                {"table": "II", "code": "M"}, {"table": "II", "code": "A"}):
        assert not _matches(row, "focus") and _matches(row, "all")
    assert _matches({"table": "II", "code": "M"}, "derivative")


def test_migration_drops_focus_from_grants_and_keeps_it_for_purchases():
    with session() as s, s.begin():
        publish(s, 1, day=date(2026, 9, 2), transaction=date(2026, 9, 1))
        publish(s, 2, day=date(2026, 9, 3), transaction=date(2026, 9, 2), code=b"A")
    configuration = Config(str(ROOT / "alembic.ini"))
    try:
        command.downgrade(configuration, "0029")
        with engine().begin() as connection:
            # Pre-0030 state: the grant group carried "focus" (as a derivative row did).
            connection.execute(text("""UPDATE feed_group_revision SET
                match_kinds = match_kinds || '["focus"]',
                transaction_sort_dates = transaction_sort_dates
                    || '{"focus": "2026-09-02", "accepted\\:focus": "2026-09-03T22:00:00+00:00"}'
                WHERE NOT match_kinds ? 'focus'"""))
            connection.execute(text("""INSERT INTO feed_group_order
                (kind, sort_order, group_key, sort_key, accepted_at, revision_id, seq, xid)
                SELECT 'focus', sort_order, group_key, sort_key, accepted_at, revision_id, seq, xid
                FROM feed_group_order o WHERE kind = 'all'
                  AND NOT EXISTS (SELECT 1 FROM feed_group_order f WHERE f.kind = 'focus'
                                  AND f.group_key = o.group_key AND f.sort_order = o.sort_order)"""))
    finally:
        command.upgrade(configuration, "head")
    with session() as s, s.begin():
        revisions = {row.accepted_at.date(): row for row in s.scalars(select(FeedRevision))}
        purchase, grant = revisions[date(2026, 9, 2)], revisions[date(2026, 9, 3)]
        assert "focus" in purchase.match_kinds and "focus" not in grant.match_kinds
        assert purchase.transaction_sort_dates["focus"] == purchase.transaction_sort_dates["buy"] == "2026-09-01"
        assert "focus" not in grant.transaction_sort_dates
        focus_groups = set(s.scalars(select(FeedGroupOrder.group_key).where(FeedGroupOrder.kind == "focus")))
        assert focus_groups == {purchase.group_key}
        assert [group["id"] for group in feed(s, kind="focus")["groups"]] == [purchase.group_key]
