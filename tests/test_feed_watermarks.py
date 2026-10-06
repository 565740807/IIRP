"""Watermark reading sessions: stable keyset pages and counted updates.

Real PostgreSQL in a disposable iirp_v1_test_* database; all filings are synthetic.
Commits happen between steps where the behaviour depends on other transactions.
"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from alembic import command
from alembic.config import Config
from iirp.business_models import (
    FeedGroupCurrent,
    FeedGroupOrder,
    FeedRevision,
    FeedSession,
    FeedWatermarkCluster,
    TransactionEvent,
)
from iirp.config import ROOT
from iirp.db import engine, session
from iirp.feed_index import decode_cursor, encode_cursor, ensure_cluster
from iirp.feed_updates import feed_updates
from iirp.sec_facts import feed, feed_group
from sqlalchemy import func, select, text
from test_sec_facts import FIXTURE, clean, isolated_database, save  # noqa: F401

FIRST_DAY = date(2026, 6, 1)

CUTOFF = datetime(2026, 8, 21, tzinfo=timezone.utc)


def publish(s, index, *, day, transaction, code=b"P", owners=(b"456", b"789")):
    """One synthetic joint Form 4 accepted on ``day``; one group per issuer and day."""
    xml = (FIXTURE.read_bytes()
           .replace(b"2026-08-31", transaction.isoformat().encode())
           .replace(b"<rptOwnerCik>456</rptOwnerCik>", b"<rptOwnerCik>" + owners[0] + b"</rptOwnerCik>")
           .replace(b"<rptOwnerCik>789</rptOwnerCik>", b"<rptOwnerCik>" + owners[1] + b"</rptOwnerCik>")
           .replace(b"<transactionCode>P</transactionCode>",
                    b"<transactionCode>" + code + b"</transactionCode>"))
    save(s, xml=xml, accession=f"0000000123-26-{index:06}", accepted=f"{day.isoformat()}T18:00:00-04:00")


def seed(groups=45):
    with session() as s, s.begin():
        for index in range(groups):
            day = FIRST_DAY + timedelta(days=index)
            publish(s, index + 1, day=day, transaction=day - timedelta(days=1))


def read_all(s, first, kind="buy"):
    pages, cursor = [first], first["next_cursor"]
    while cursor:
        pages.append(feed(s, first["session_id"], cursor, kind=kind))
        cursor = pages[-1]["next_cursor"]
    return pages


def ids(pages):
    return [group["id"] for page in pages for group in page["groups"]]


def test_keyset_pages_never_shift_while_new_filings_publish():
    seed()
    with session() as s, s.begin():
        reference = ids(read_all(s, feed(s, kind="buy")))
        opened = feed(s, kind="buy")
    assert len(reference) == len(set(reference)) == 45 and ids([opened]) == reference[:20]
    third_day = FIRST_DAY + timedelta(days=2)
    with session() as s, s.begin():
        # A newest group (would lead page 1), a page-3 group revised with another
        # trade, and a late disclosure of an old trade landing inside page 2.
        # Other reporting persons: none of these may look like a duplicate filing.
        publish(s, 100, day=date(2026, 9, 20), transaction=date(2026, 9, 19), owners=(b"901", b"902"))
        publish(s, 101, day=third_day, transaction=third_day, owners=(b"901", b"902"))
        publish(s, 102, day=date(2026, 9, 21), transaction=FIRST_DAY + timedelta(days=20), owners=(b"901", b"902"))
    with session() as s, s.begin():
        pages = read_all(s, opened)
        assert ids(pages) == reference
        assert [len(page["groups"]) for page in pages] == [20, 20, 5]
        assert pages[-1]["next_cursor"] is None
        assert all(page["total_groups"] == 45 for page in pages)
        revised = next(g for g in pages[-1]["groups"] if g["accepted_date"] == str(third_day))
        assert revised["matching_transactions"] == 1
        assert feed_updates(s, opened["session_id"])["new_count"] == 3
        applied = feed_updates(s, opened["session_id"], include_groups=True)
        assert applied["new_count"] == 3
        assert [g["accepted_date"] for g in applied["groups"]] == ["2026-09-20", "2026-09-21", str(third_day)]
        assert applied["groups"][2]["matching_transactions"] == 2
        assert feed_updates(s, applied["target_session_id"])["new_count"] == 0
        fresh = read_all(s, feed(s, kind="buy"))
        assert fresh[0]["total_groups"] == 47 and len(ids(fresh)) == len(set(ids(fresh))) == 47
        assert fresh[0]["groups"][0]["accepted_date"] == "2026-09-20"


def test_inflight_publication_is_counted_not_leaked_into_an_open_session():
    seed(3)
    writer = session()
    writer.begin()
    try:
        publish(writer, 200, day=date(2026, 9, 22), transaction=date(2026, 9, 21))
        with session() as s, s.begin():
            opened = feed(s, kind="buy")  # The writer's revision is not committed yet.
        writer.commit()
    finally:
        writer.close()
    with session() as s, s.begin():
        again = feed(s, opened["session_id"], kind="buy")
        assert ids([again]) == ids([opened]) and again["total_groups"] == 3
        assert feed_updates(s, opened["session_id"])["new_count"] == 1
        assert feed(s, kind="buy")["total_groups"] == 4


def test_reading_sessions_store_only_a_watermark_and_never_a_manifest():
    seed(25)
    with session() as s, s.begin():
        first = feed(s, kind="all")
        feed(s, first["session_id"], first["next_cursor"], kind="all")
        feed(s, kind="sell")
        for saved in s.scalars(select(FeedSession)):
            assert saved.revision_ids == []
            assert set(saved.filters["watermark"]) <= {"seq", "snapshot", "own"}
            assert len(json.dumps(saved.filters)) < 1000


def test_cursor_round_trip_and_rejection():
    seed(21)
    with session() as s, s.begin():
        first = feed(s, kind="buy")
        key = decode_cursor(first["next_cursor"])
        assert encode_cursor(key) == first["next_cursor"]
        for bad in ("20", "k1.!!", "k1." + "A" * 400):
            with pytest.raises(ValueError, match="游标"):
                feed(s, first["session_id"], bad, kind="buy")


def test_migration_backfills_pointers_from_each_groups_newest_revision():
    with session() as s, s.begin():
        publish(s, 1, day=date(2026, 9, 1), transaction=date(2026, 8, 31))
        publish(s, 2, day=date(2026, 8, 20), transaction=date(2026, 8, 19))
        publish(s, 3, day=date(2026, 8, 20), transaction=date(2026, 8, 18), code=b"S")
    configuration = Config(str(ROOT / "alembic.ini"))
    try:
        command.downgrade(configuration, "0020")
        with engine().begin() as connection:
            assert "seq" not in {row[0] for row in connection.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name='feed_group_revision'"))}
    finally:
        command.upgrade(configuration, "head")
    with session() as s, s.begin():
        assert s.scalar(select(func.count()).select_from(FeedGroupCurrent)) == 2
        assert s.scalar(select(func.count()).select_from(FeedRevision).where(FeedRevision.seq.is_not(None))) == 0
        joint = s.scalar(select(FeedRevision).where(FeedRevision.accepted_at < CUTOFF)
                         .order_by(FeedRevision.created_at.desc()).limit(1))
        assert s.get(FeedGroupCurrent, joint.group_key).revision_id == joint.id
        keys = {(row.kind, row.sort_order): row.sort_key for row in s.scalars(
            select(FeedGroupOrder).where(FeedGroupOrder.group_key == joint.group_key))}
        assert {kind for kind, _ in keys} == set(joint.match_kinds)
        assert keys[("buy", "transaction")].date() == date(2026, 8, 19)
        assert keys[("sell", "transaction")].date() == date(2026, 8, 18)
        assert keys[("sell", "accepted")] == joint.accepted_at
        listed = feed(s, kind="all")
        assert [group["accepted_date"] for group in listed["groups"]] == ["2026-09-01", "2026-08-20"]
        # New publications after the upgrade get sequence numbers and pointers.
        publish(s, 4, day=date(2026, 9, 2), transaction=date(2026, 9, 1))
        newest = s.scalar(select(FeedGroupCurrent).order_by(FeedGroupCurrent.seq.desc().nulls_last()).limit(1))
        assert newest.seq is not None and s.get(FeedRevision, newest.revision_id).accepted_at.date() == date(2026, 9, 2)


def test_tombstone_leaves_listing_but_keeps_history_for_open_sessions():
    seed(2)
    with session() as s, s.begin():
        opened = feed(s, kind="buy")
        victim = opened["groups"][0]
    with session() as s, s.begin():
        from iirp.sec_facts import _refresh_groups

        for event in s.scalars(select(TransactionEvent).where(TransactionEvent.issuer_id == victim["issuer_id"])):
            if str(event.accepted_at.date()) >= victim["accepted_date"]:
                event.status = "SUPERSEDED"
        s.flush()
        _refresh_groups(s, {(victim["issuer_id"], date.fromisoformat(victim["accepted_date"]))})
    with session() as s, s.begin():
        assert ids([feed(s, opened["session_id"], kind="buy")]) == ids([opened])
        assert feed_group(s, opened["session_id"], victim["id"])["total"] == 1
        assert victim["id"] not in ids([feed(s, kind="buy")])
        removed = feed_updates(s, opened["session_id"], include_groups=True)
        assert removed["removed_ids"] == [victim["id"]] and removed["groups"] == []


def test_restore_into_another_cluster_forgets_foreign_transaction_ids():
    seed(3)
    with session() as s, s.begin():
        opened = feed(s, kind="buy")
        assert ensure_cluster(s) is False  # Same cluster: nothing changes.
        assert s.scalar(select(func.count()).select_from(FeedRevision).where(FeedRevision.xid.is_not(None))) == 3
        # Simulate a dump restored on another machine: foreign ids look "future".
        s.get(FeedWatermarkCluster, 1).system_identifier = "another-cluster"
        s.execute(text("UPDATE feed_group_revision SET xid = '999999999'::xid8"))
        s.execute(text("UPDATE feed_group_current SET xid = '999999999'::xid8"))
        s.execute(text("UPDATE feed_group_order SET xid = '999999999'::xid8"))
    with session() as s, s.begin():
        assert feed(s, kind="buy")["total_groups"] == 0  # What the bug would look like.
        assert ensure_cluster(s) is True
        assert s.get(FeedSession, opened["session_id"]) is None
        for table in (FeedRevision, FeedGroupCurrent, FeedGroupOrder):
            assert s.scalar(select(func.count()).select_from(table).where(table.xid.is_not(None))) == 0
    with session() as s, s.begin():
        assert feed(s, kind="buy")["total_groups"] == 3
