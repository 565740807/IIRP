"""Transactions dated after their own SEC acceptance date: kept, flagged, never sorted.

Real PostgreSQL in a disposable iirp_v1_test_* database; all filings are synthetic.
"""

from datetime import date, datetime, timezone

from alembic import command
from alembic.config import Config
from iirp.config import ROOT
from iirp.db import engine, session
from iirp.insider.entities import read_entity_history
from iirp.insider.feed import feed, feed_group
from iirp.insider.feed_index import SORT_MISSING
from iirp.insider.records import transaction_record
from iirp.messages import msg
from iirp.models import FeedGroupOrder, FeedRevision
from sqlalchemy import select, text

from tests.insider.test_feed_watermarks import publish
from tests.sec.test_sec_facts import clean, isolated_database  # noqa: F401
from tests.zh import zh

CUTOFF = datetime(2026, 8, 21, tzinfo=timezone.utc)


def test_transaction_dated_after_acceptance_is_flagged_and_not_sorted_or_ranged():
    with session() as s, s.begin():
        publish(s, 1, day=date(2026, 9, 1), transaction=date(2026, 8, 31))
        # A mistyped year: accepted 2026-08-20, reported trade in 2036.
        publish(s, 2, day=date(2026, 8, 20), transaction=date(2036, 8, 19))
    with session() as s, s.begin():
        groups = feed(s, kind="buy")["groups"]
        assert [group["accepted_date"] for group in groups] == ["2026-09-01", "2026-08-20"]
        anomalous = groups[1]
        row = anomalous["transactions"][0]
        assert row["transaction_date"] == "2036-08-19"  # The reported value is kept.
        assert row["date_anomaly"] == {
            "code": "TRANSACTION_AFTER_ACCEPTANCE", "label": msg("insider.date_anomaly"),
            "transaction_date": "2036-08-19", "accepted_date": "2026-08-20",
        }
        assert anomalous["transaction_dates"] == []
        assert "date_anomaly" not in groups[0]["transactions"][0]
        order_key = s.scalar(select(FeedGroupOrder.sort_key).where(
            FeedGroupOrder.group_key == anomalous["id"], FeedGroupOrder.kind == "buy",
            FeedGroupOrder.sort_order == "transaction"))
        assert order_key == SORT_MISSING
        detail = transaction_record(s, row["id"])
        assert detail["transaction_date"] == "2036-08-19" and zh(detail["date_anomaly"]["label"]) == "日期异常、待核对"
        group_rows = feed_group(s, feed(s, kind="buy")["session_id"], anomalous["id"])["items"]
        assert group_rows[0]["date_anomaly"]["transaction_date"] == "2036-08-19"
        history = read_entity_history(s, "company", anomalous["issuer_id"])["data"]
        assert history["transaction_end"] == "2026-08-31"
        assert history["items"][0]["transaction_date"] == "2026-08-31"
        assert history["items"][-1]["date_anomaly"]["transaction_date"] == "2036-08-19"


def test_migration_recomputes_sort_dates_led_by_an_anomalous_date():
    with session() as s, s.begin():
        publish(s, 1, day=date(2026, 9, 1), transaction=date(2026, 8, 31))
        publish(s, 2, day=date(2026, 8, 20), transaction=date(2036, 8, 19))
        publish(s, 3, day=date(2026, 8, 20), transaction=date(2026, 8, 18), code=b"S")
    configuration = Config(str(ROOT / "alembic.ini"))
    try:
        command.downgrade(configuration, "0021")
        with engine().begin() as connection:
            # The pre-0022 state: stored sort dates led by the mistyped year.
            connection.execute(text("""UPDATE feed_group_revision SET transaction_sort_dates =
                transaction_sort_dates || '{"all": "2036-08-19", "buy": "2036-08-19"}'
                WHERE accepted_at < '2026-08-21'"""))
            connection.execute(text("""UPDATE feed_group_order SET sort_key = '2036-08-19 00:00+00'
                WHERE kind IN ('all', 'buy') AND sort_order = 'transaction'
                  AND accepted_at < '2026-08-21'"""))
    finally:
        command.upgrade(configuration, "head")
    with session() as s, s.begin():
        fixed = s.scalar(select(FeedRevision).where(FeedRevision.accepted_at < CUTOFF)
                         .order_by(FeedRevision.seq.desc().nulls_last()).limit(1))
        assert fixed.transaction_sort_dates["buy"] == ""
        assert fixed.transaction_sort_dates["sell"] == fixed.transaction_sort_dates["all"] == "2026-08-18"
        keys = {(row.kind, row.sort_order): row.sort_key for row in s.scalars(
            select(FeedGroupOrder).where(FeedGroupOrder.group_key == fixed.group_key))}
        assert keys[("buy", "transaction")] == SORT_MISSING
        assert keys[("all", "transaction")].date() == date(2026, 8, 18)
        assert [group["accepted_date"] for group in feed(s, kind="all")["groups"]] == ["2026-09-01", "2026-08-20"]
