"""Filter-consistent facts and stable transaction-first pagination."""

import runpy
from datetime import date, datetime

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from iirp.config import ROOT
from iirp.db import session
from iirp.insider.feed import feed, feed_group
from iirp.insider.records import resolve_amendment, transaction_record
from iirp.insider.views import _refresh_groups
from iirp.models import AmendmentRelation, FeedRevision, TransactionEvent
from sqlalchemy import select

from tests.sec.test_sec_facts import FIXTURE, clean, isolated_database, save  # noqa: F401
from tests.zh import zh


def test_filter_scope_counts_dates_filing_roles_and_joint_totals_match():
    with session() as s, s.begin():
        save(s)
        other = (
            FIXTURE.read_bytes()
            .replace(
                b"<transactionCode>P</transactionCode>", b"<transactionCode>S</transactionCode>"
            )
            .replace(b"2026-08-31", b"2026-08-29")
            .replace(b"<rptOwnerCik>456</rptOwnerCik>", b"<rptOwnerCik>999</rptOwnerCik>")
        )
        save(s, xml=other, accession="0000000123-26-000002")
        bought = feed(s, kind="buy")["groups"][0]
        sold = feed(s, kind="sell")["groups"][0]
        assert bought["transaction_dates"] == ["2026-08-31"]
        assert sold["transaction_dates"] == ["2026-08-29"]
        assert bought["owners"] == sold["owners"] == 2
        assert bought["filings"] == sold["filings"] == 1
        assert bought["summary"][0]["shares"] == "100000"
        assert bought["summary"][0]["owner_count"] == 2
        assert any(owner["roles"] for owner in bought["transactions"][0]["owners"])


def test_transaction_sort_happens_before_group_and_detail_pagination_and_freezes():
    with session() as s, s.begin():
        for index in range(24):
            xml = FIXTURE.read_bytes().replace(b"2026-08-31", f"2026-08-{index + 1:02}".encode())
            save(s, xml=xml, accession=f"0000000123-26-{index + 1:06}")
        first = feed(s, kind="buy")
        group = first["groups"][0]
        second = feed_group(s, first["session_id"], group["id"], "20")
        rows = group["transactions"] + second["items"]
        assert len(rows) == len({row["id"] for row in rows}) == 24
        assert [row["transaction_date"] for row in rows] == [
            f"2026-08-{day:02}" for day in range(24, 0, -1)
        ]
        # Later disclosure of an old trade must not jump above a newer actual trade.
        xml = FIXTURE.read_bytes().replace(b"2026-08-31", b"2024-03-15")
        save(s, xml=xml, accession="0000000123-26-000025", accepted="2026-09-08T21:00:00-04:00")
        assert feed(s, kind="buy")["groups"][0]["accepted_date"] == "2026-09-01"
        assert feed(s, kind="buy", order="accepted")["groups"][0]["accepted_date"] == "2026-09-08"
        assert feed(s, first["session_id"], kind="buy")["groups"] == first["groups"]
        with pytest.raises(ValueError, match="feed.filters_changed"):
            feed(s, first["session_id"], kind="buy", order="accepted")


def test_unconfirmed_amendment_has_reported_values_but_no_confirmed_total():
    with session() as s, s.begin():
        save(s)
        event = s.scalar(
            select(TransactionEvent).where(TransactionEvent.data["code"].astext == "P")
        )
        event.status = "NEEDS_REVIEW"
        event.transaction_date = date(2024, 3, 15)
        event.data = {**event.data, "form": "4/A", "shares": "1185", "price_per_share": "8.50"}
        s.flush()
        _refresh_groups(s, {(event.issuer_id, date(2026, 9, 1))})
        result = feed(s, kind="buy")["groups"][0]
        assert result["transaction_dates"] == ["2024-03-15"]
        assert result["summary"][0]["shares"] is None
        assert result["summary"][0]["known_amount"] is None
        assert result["summary"][0]["reported_value_review_rows"] == 1
        assert result["summary"][0]["missing_source_value_rows"] == 0
        assert result["transactions"][0]["price"] == "8.50"
        assert "源申报数值" in zh(result["transactions"][0]["summary_exclusion_reason"])


def test_legacy_transaction_detail_reads_roles_from_its_filing_without_admitting_amendment():
    with session() as s, s.begin():
        save(s)
        event = s.scalar(select(TransactionEvent).where(TransactionEvent.data["code"].astext == "P"))
        event.status = "NEEDS_REVIEW"
        # Older persisted transactions predate the inline relationship cache;
        # the original filing still contains the director observation.
        event.data = {key: value for key, value in event.data.items() if key != "owner_relationships"}
        event.data = {**event.data, "shares": "1185", "price_per_share": "8.50"}
        s.flush()
        original_data = dict(event.data)
        record = transaction_record(s, event.id)
        director = next(owner for owner in record["owners"] if owner["id"] == "0000000456")
        assert [zh(role) for role in director["roles"]] == ["董事"]
        assert director["entity_type"] == "person"
        assert record["shares"] == "1185" and record["price"] == "8.50"
        assert "源申报数值暂未计入确认汇总" in zh(record["summary_exclusion_reason"])
        assert record["amount"] is None and record["eligible_for_totals"] is False
        assert event.status == "NEEDS_REVIEW" and event.data == original_data


def test_filtered_recent_disclosures_sort_by_the_same_visible_time_and_freeze():
    with session() as s, s.begin():
        raw = FIXTURE.read_bytes()
        save(s, xml=raw, accession="0000000123-26-000101", accepted="2026-09-01T09:00:00-04:00")
        save(
            s,
            xml=raw.replace(
                b"<transactionCode>P</transactionCode>", b"<transactionCode>S</transactionCode>"
            ),
            accession="0000000123-26-000102",
            accepted="2026-09-01T16:00:00-04:00",
        )
        second = raw.replace(b"<issuerCik>123</issuerCik>", b"<issuerCik>124</issuerCik>")
        save(s, xml=second, accession="0000000124-26-000103", accepted="2026-09-01T15:00:00-04:00")
        result = feed(s, kind="buy", order="accepted")
        assert len(result["groups"]) == 2
        times = [group["accepted_at"] for group in result["groups"]]
        assert times == ["2026-09-01T19:00:00+00:00", "2026-09-01T13:00:00+00:00"]
        # Exercise an actual populated 0007 -> 0008 backfill, including the
        # immutability of the already opened reading session and the JSON facts.
        revision_ids = [group["revision_id"] for group in result["groups"]]
        revisions = s.scalars(select(FeedRevision).where(FeedRevision.id.in_(revision_ids))).all()
        expected = {row.id: (dict(row.transaction_sort_dates), dict(row.data)) for row in revisions}
        migration = runpy.run_path(
            str(ROOT / "migrations/versions/0008_feed_filtered_disclosure_sort.py")
        )
        with Operations.context(MigrationContext.configure(s.connection())):
            migration["downgrade"]()
            s.expire_all()
            assert all(
                not any(key.startswith("accepted:") for key in row.transaction_sort_dates)
                for row in revisions
            )
            migration["upgrade"]()
        s.expire_all()
        for row in revisions:
            before, facts_before = expected[row.id]
            assert row.data == facts_before
            for key, value in before.items():
                if key.startswith("accepted:"):

                    assert datetime.fromisoformat(
                        row.transaction_sort_dates[key]
                    ) == datetime.fromisoformat(value)
                else:
                    assert row.transaction_sort_dates[key] == value
        assert [
            group["accepted_at"] for group in feed(s, kind="buy", order="accepted")["groups"]
        ] == times
        save(s, xml=raw, accession="0000000123-26-000104", accepted="2026-09-01T17:00:00-04:00")
        assert (
            feed(s, kind="buy", order="accepted")["groups"][0]["accepted_at"]
            == "2026-09-01T21:00:00+00:00"
        )
        assert (
            feed(s, result["session_id"], kind="buy", order="accepted")["groups"]
            == result["groups"]
        )


def test_amendment_summary_includes_rows_beyond_first_detail_page():
    with session() as s, s.begin():
        for index in range(22):
            raw = FIXTURE.read_bytes().replace(b"2026-08-31", f"2026-08-{index + 1:02}".encode())
            save(s, xml=raw, accession=f"0000000123-26-{index + 1:06}")
        oldest = s.scalar(
            select(TransactionEvent)
            .where(TransactionEvent.data["code"].astext == "P")
            .order_by(TransactionEvent.transaction_date)
        )
        oldest.status = "NEEDS_REVIEW"
        oldest.data = {**oldest.data, "form": "4/A"}
        s.flush()
        _refresh_groups(s, {(oldest.issuer_id, date(2026, 9, 1))})
        group = feed(s, kind="buy")["groups"][0]
        assert len(group["transactions"]) == 20
        assert all(row["form"] == "4" for row in group["transactions"])
        assert group["amendment_count"] == 1
        assert group["summary"][0]["reported_value_review_rows"] == 1


def test_repeated_amendments_keep_original_disclosure_identity_and_one_total():
    with session() as s, s.begin():
        save(s)
        current = s.scalar(
            select(TransactionEvent).where(TransactionEvent.data["code"].astext == "P")
        )
        first_accession, first_accepted = current.accession, current.accepted_at.isoformat()
        for index, price in [(2, b"26.00"), (3, b"27.00")]:
            accession = f"0000000123-26-{index:06}"
            xml = (
                FIXTURE.read_bytes()
                .replace(b"<documentType>4</documentType>", b"<documentType>4/A</documentType>")
                .replace(b"<value>25.25</value>", b"<value>" + price + b"</value>")
            )
            save(s, xml, accession, f"2026-09-{index:02}T18:00:00-04:00")
            corrected = s.scalar(
                select(TransactionEvent).where(
                    TransactionEvent.accession == accession,
                    TransactionEvent.data["code"].astext == "P",
                )
            )
            relation = s.scalar(
                select(AmendmentRelation).where(
                    AmendmentRelation.amended_event_id == corrected.id,
                    AmendmentRelation.action == "UNCONFIRMED",
                )
            )
            resolve_amendment(
                s,
                relation.id,
                "replace",
                current.id,
                {"note": "SYNTHETIC explicitly reviewed second amendment chain"},
            )
            current = corrected
        groups = feed(s, kind="buy")["groups"]
        original_group = next(g for g in groups if g["accepted_date"] == "2026-09-01")
        assert original_group["transactions"][0]["group_accession"] == first_accession
        assert original_group["transactions"][0]["group_accepted_at"] == first_accepted
        assert original_group["accepted_at"] == first_accepted
        assert original_group["summary"][0]["known_amount"] == "2700000.00"
        assert all(
            g["summary"][0]["known_amount"] is None for g in groups if g is not original_group
        )
