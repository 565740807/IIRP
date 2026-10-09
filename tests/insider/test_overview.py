"""Insider overview rules against isolated PostgreSQL and independent price examples."""

import importlib.util
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from iirp.api.app import app
from iirp.config import ROOT
from iirp.db import session
from iirp.insider.feed import feed as read_feed
from iirp.insider.feed_updates import feed_updates
from iirp.insider.overview import overview
from iirp.insider.prices import effective_price, estimate_prices, feed_prices, price_comparison
from iirp.insider.typed import typed_fields, weighted_price_range
from iirp.insider.views import _event_view, _refresh_groups, _trader_key
from iirp.messages import UserError
from iirp.models import (
    FeedRevision,
    FeedSession,
    Filing,
    FilingOwner,
    FilingVersion,
    IndexConstituent,
    Issuer,
    Owner,
    PriceCache,
    PriceCacheBar,
    Security,
    SourceObject,
    TransactionEvent,
    now,
)
from sqlalchemy import select

from tests.jobs.test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401

AT = datetime(2026, 10, 8, 18, tzinfo=timezone.utc)
DAY = date(2026, 10, 8)


def seed(s, *, ticker="TEST", issuer="0000000100", day=DAY, owners=("0000000200",),
         code="P", price="10", shares="100", roles=None, plan=False, status="CURRENT",
         visible=True, accepted=AT, data=None):
    """Each event has a synthetic filing with explicit filing-time relationships."""
    if s.get(Issuer, issuer) is None:
        s.add(Issuer(id=issuer, name=f"Issuer {ticker}", ticker=ticker))
        s.flush()
    accession = uuid.uuid4().hex[:20]
    source = uuid.uuid4().hex * 2
    s.add(SourceObject(sha256=source, relative_path=f"synthetic/{source}", byte_size=0,
                       media_type="application/xml"))
    s.add(Filing(accession=accession, form="4", issuer_id=issuer, accepted_at=accepted,
                 visible=visible))
    s.flush()
    version = FilingVersion(accession=accession, source_hash=source,
                            parser_version="synthetic", data={})
    s.add(version)
    s.flush()
    relationships = []
    for owner in owners:
        if s.get(Owner, owner) is None:
            s.add(Owner(id=owner, name=f"Owner {owner}"))
            s.flush()
        relationship = dict(roles or {})
        relationships.append(relationship)
        s.add(FilingOwner(version_id=version.id, owner_id=owner, relationship=relationship))
    original = {"table": "I", "code": code, "direction": "A" if code == "P" else "D",
                "shares": shares, "price_per_share": price, "direct_or_indirect": "D",
                "raw_10b5_1_flag": "1" if plan else "0", **(data or {})}
    event = TransactionEvent(issuer_id=issuer, accession=accession, version_id=version.id,
                             row_key="I:0", transaction_date=day, accepted_at=accepted,
                             owner_ids=list(owners), data=original, status=status,
                             **typed_fields(original, relationships))
    s.add(event)
    s.flush()
    return event


def read(**kwargs):
    with session() as s:
        return overview(s, at=AT, **kwargs)


@pytest.mark.parametrize("days,window,people", [(7, 7, 2), (30, 14, 3), (90, 30, 2)])
def test_overview_browser_query_numbers(days, window, people):
    with TestClient(app) as client:
        response = client.get("/api/v1/insider/overview", params={
            "days": days, "cluster_days": window, "cluster_people": people,
            "index": "sp500", "role": "director", "min_amount": "1000",
            "exclude_plans": "true", "exclude_cluster_plans": "false",
        })
    assert response.status_code == 200
    assert response.json()["companies"]["items"] == []


@pytest.mark.parametrize("parameter,value", [("days", "14"), ("cluster_days", "90"),
                                              ("cluster_people", "4"), ("days", "7.0")])
def test_overview_rejects_unsupported_browser_query(parameter, value):
    with TestClient(app) as client:
        response = client.get("/api/v1/insider/overview", params={parameter: value})
    assert response.status_code == 422


@pytest.mark.parametrize("offset,qualifies", [(6, True), (7, False)])
def test_cluster_seven_calendar_day_boundary(offset, qualifies):
    with session() as s, s.begin():
        seed(s, owners=("0000000200",))
        seed(s, owners=("0000000201",), day=DAY - timedelta(days=offset))
    result = read()
    assert bool(result["cluster_buys"]["items"]) is qualifies
    if qualifies:
        company = result["cluster_buys"]["items"][0]
        assert company["buy_people"] == 2
        assert Decimal(company["buy_amount"]) == 2000
        assert company["start_date"] == "2026-10-02"
        assert company["end_date"] == "2026-10-08"


def test_cluster_counts_distinct_owners_and_each_joint_transaction_amount_once():
    with session() as s, s.begin():
        seed(s, owners=("0000000200", "0000000201"), shares="100", price="10")
        seed(s, owners=("0000000200",), shares="200", price="20")
    result = read()
    company = result["cluster_buys"]["items"][0]
    assert company["buy_people"] == 2
    assert Decimal(company["buy_amount"]) == 5000
    assert company["buy_price"]["price"] == "16.67"
    assert read(cluster_people=3)["cluster_buys"]["items"] == []
    assert Decimal(result["companies"]["items"][0]["net_amount"]) == 5000


def test_repeated_owner_is_not_multiple_insiders():
    with session() as s, s.begin():
        for offset in (0, 1, 2):
            seed(s, day=DAY - timedelta(days=offset))
    assert read()["cluster_buys"]["items"] == []


def test_sale_clusters_exclude_plan_trades_by_default_and_toggle_explicitly():
    with session() as s, s.begin():
        seed(s, code="S", owners=("0000000200",))
        seed(s, code="S", owners=("0000000201",), plan=True)
    assert read()["cluster_sales"]["items"] == []
    included = read(exclude_cluster_plans=False)
    assert included["cluster_sales"]["items"][0]["sell_people"] == 2
    assert Decimal(included["cluster_sales"]["items"][0]["sell_amount"]) == 2000
    excluded = read(exclude_plans=True, exclude_cluster_plans=False)
    assert len(excluded["large_sales"]["items"]) == 1
    assert excluded["companies"]["items"][0]["sell_people"] == 1
    assert excluded["cluster_sales"]["items"] == []


def test_all_sections_obey_index_role_amount_and_time_filters():
    with session() as s, s.begin():
        ceo = seed(s, issuer="0000000101", ticker="CEO", price="30", roles={
            "is_officer": "1", "officer_title": "CEO"}, data={"shares_after": "500"})
        director = seed(s, issuer="0000000102", ticker="DIR", roles={"is_director": "1"})
        holder = seed(s, issuer="0000000103", ticker="TEN", roles={"is_ten_percent_owner": "1"})
        seed(s, issuer="0000000104", ticker="OLD", day=DAY - timedelta(days=30), price="1000")
        s.add(IndexConstituent(index_name="sp500", ticker="OTHER", cik=ceo.issuer_id,
                               name="CEO company", industry="Technology", updated_at=AT))
        s.add(IndexConstituent(index_name="nasdaq100", ticker="DIR", cik=None,
                               name="Director company", industry="Technology", updated_at=AT))
        ids = {"executive": ceo.id, "director": director.id, "ten_percent": holder.id}
    assert len(read()["companies"]["items"]) == 3
    assert len(read(days=90)["companies"]["items"]) == 4
    for role, identifier in ids.items():
        filtered = read(role=role)
        assert [item["id"] for item in filtered["large_buys"]["items"]] == [identifier]
        assert len(filtered["companies"]["items"]) == 1
    assert read(index="sp500")["companies"]["items"][0]["ticker"] == "CEO"
    assert read(index="nasdaq100")["companies"]["items"][0]["ticker"] == "DIR"
    assert read(index="sp500", role="director")["companies"]["items"] == []
    assert [item["id"] for item in read(min_amount=Decimal(3000))["large_buys"]["items"]] == [ids["executive"]]
    assert read(min_amount=Decimal(3001))["companies"]["items"] == []


def test_large_top_ten_sort_and_company_net_sort_are_backend_calculations():
    with session() as s, s.begin():
        for number in range(1, 13):
            seed(s, issuer=f"{number:010d}", ticker=f"T{number}", price=str(number))
        seed(s, issuer="0000000012", ticker="T12", code="S", price="20")
    result = read()
    assert len(result["large_buys"]["items"]) == 10
    assert [Decimal(item["amount"]) for item in result["large_buys"]["items"]] == [Decimal(number * 100) for number in range(12, 2, -1)]
    assert result["companies"]["items"][0]["ticker"] == "T11"
    assert result["companies"]["items"][-1]["ticker"] == "T12"
    assert Decimal(result["companies"]["items"][-1]["net_amount"]) == -800


@pytest.mark.parametrize("kwargs", [
    {"status": "NEEDS_REVIEW"}, {"status": "SUPERSEDED"}, {"visible": False},
    {"day": DAY + timedelta(days=1)}, {"day": DAY, "accepted": AT - timedelta(days=1)},
    {"code": "A"}, {"data": {"table": "II"}}, {"data": {"currency": "EUR"}},
])
def test_ineligible_facts_never_enter_any_overview_section(kwargs):
    with session() as s, s.begin():
        seed(s, **kwargs)
    result = read()
    for section in ("cluster_buys", "cluster_sales", "large_buys", "large_sales",
                    "executive_buys", "holding_increases", "companies"):
        assert result[section]["items"] == []


def test_holding_increase_twenty_percent_boundary_and_executive_filing_roles():
    with session() as s, s.begin():
        boundary = seed(s, roles={"is_officer": "1", "officer_title": "Chairman"},
                        data={"shares_after": "600"})
        seed(s, data={"shares_after": "601"})
        seed(s, data={"shares_after": "100"})
        seed(s, data={"shares_after": None})
        seed(s, roles={"is_officer": "1", "officer_title": "Vice President"})
    result = read()
    assert [item["id"] for item in result["holding_increases"]["items"]] == [boundary.id]
    assert Decimal(result["holding_increases"]["items"][0]["holding_change"]) == 20
    assert [item["id"] for item in result["executive_buys"]["items"]] == [boundary.id]


def test_typical_price_uses_full_ohlc_only_live_valid_daily_cache():
    with session() as s, s.begin():
        trade = seed(s, price=None, shares="12")
        security = Security(symbol="TEST", issuer_id=trade.issuer_id)
        s.add(security)
        s.flush()
        cache = PriceCache(security_id=security.id, start_date=DAY, end_date=DAY,
                           complete_through=DAY, expires_at=now() + timedelta(hours=1))
        s.add(cache)
        s.flush()
        s.add(PriceCacheBar(cache_id=cache.id, session_date=DAY, high=15, low=9, close=12,
                            status="VALID"))
    result = read()
    item = result["large_buys"]["items"][0]
    assert Decimal(item["price"]["price"]) == 12
    assert item["price"]["estimated"] and item["amount_estimated"]
    assert Decimal(item["price"]["range_low"]) == 9
    assert Decimal(item["price"]["range_high"]) == 15
    assert Decimal(item["amount"]) == 144
    with session() as s, s.begin():
        cache = s.scalar(select(PriceCache))
        cache.expires_at = now() - timedelta(seconds=1)
    with session() as s:
        assert estimate_prices(s, [("TEST", DAY)]) == {}
    result = read()
    assert result["large_buys"]["items"] == []
    assert result["companies"]["items"][0]["buy_amount"] is None
    assert result["price_keys"] == [{"ticker": "TEST", "date": DAY.isoformat(), "issuer_id": "0000000100"}]


def test_reported_price_wins_over_daily_estimate_and_keeps_reported_range():
    price = effective_price({"ticker": "TEST", "transaction_date": DAY, "reported_price": Decimal(10),
                             "price_range_low": Decimal("9.9"), "price_range_high": Decimal("10.1")},
                            {("TEST", DAY): (Decimal(12), Decimal(9), Decimal(15))})
    assert price == (Decimal(10), False, Decimal("9.9"), Decimal("10.1"))


@pytest.mark.parametrize("current,relation,change", [
    ("101", "near", "1"), ("99", "near", "-1"), ("100", "near", "0"),
    ("101.01", "higher", "1.01"), ("98.99", "lower", "-1.01"),
])
def test_price_comparison_near_is_inclusive_and_uses_signed_percent(current, relation, change):
    result = price_comparison(Decimal(100), {"value": current, "status": "CLOSED", "as_of": "2026-10-07"})
    assert result["relation"] == relation
    assert Decimal(result["change_percent"]) == Decimal(change)
    assert result["quote_date"] == "2026-10-07" and result["quote_status"] == "closed"


def test_weighted_price_range_parses_explicit_range_without_guessing():
    assert weighted_price_range([{"text": "Weighted average price. Sales ranging from $12.10 to $12.50."}]) == (Decimal("12.10"), Decimal("12.50"))
    for text in (
        "Prices ranging from $12.10 to $12.50.",
        "Weighted average price $12.30.",
        "Weighted average: ranging from $12.50 to $12.10.",
        "Weighted average: ranging from $12.10 to $12.50, and ranging from $13.10 to $13.50.",
    ):
        assert weighted_price_range([{"text": text}]) == (None, None)


def test_migration_backfill_preserves_raw_values_and_uses_filing_owner_roles():
    with session() as s, s.begin():
        event = seed(s, owners=("0000000200", "0000000201"), shares="25.5", price="12.30",
                     roles={"is_officer": "1", "officer_title": "CFO", "is_director": "1"},
                     data={"direct_or_indirect": "I", "raw_10b5_1_flag": "1", "extra_raw": "keep",
                           "footnotes": [{"text": "Weighted average; ranging from $12.10 to $12.50."}]})
        identifier, raw = event.id, dict(event.data)
        for key in typed_fields(event.data):
            setattr(event, key, False if key.startswith("is_") else None)
    spec = importlib.util.spec_from_file_location("migration_s5c", ROOT / "migrations/versions/0003_insider_overview.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with session() as s, s.begin():
        migration.backfill(s.connection())
    with session() as s:
        event = s.get(TransactionEvent, identifier)
        assert event.data == raw and event.owner_ids == ["0000000200", "0000000201"]
        assert (event.transaction_code, event.trade_direction, event.ownership_type) == ("P", "A", "I")
        assert event.trade_shares == Decimal("25.5") and event.reported_price == Decimal("12.30")
        assert event.reported_amount == Decimal("313.65")
        assert event.is_plan and event.is_cfo and event.is_director
        assert not event.is_ceo and not event.is_president and not event.is_ten_percent
        assert (event.price_range_low, event.price_range_high) == (Decimal("12.10"), Decimal("12.50"))


def test_feed_membership_is_kept_in_session_and_update_target_filters():
    with session() as s, s.begin():
        seed(s, ticker="SP", issuer="0000000101", data={"ticker": "SP"})
        seed(s, ticker="NDX", issuer="0000000102", data={"ticker": "NDX"})
        _refresh_groups(s, {("0000000101", DAY), ("0000000102", DAY)})
        s.add(IndexConstituent(index_name="sp500", ticker="OLDSP", cik="0000000101",
                               name="SP company", updated_at=AT))
        s.add(IndexConstituent(index_name="nasdaq100", ticker="NDX", cik=None,
                               name="NDX company", updated_at=AT))
    with session() as s, s.begin():
        all_feed = read_feed(s, index="all")
        sp = read_feed(s, index="sp500")
        ndx = read_feed(s, index="nasdaq100")
        assert all_feed["total_groups"] == 2
        assert sp["total_groups"] == ndx["total_groups"] == 1
        assert sp["groups"][0]["issuer_id"] == "0000000101"
        assert ndx["groups"][0]["issuer_id"] == "0000000102"
    next_day = DAY + timedelta(days=1)
    with session() as s, s.begin():
        seed(s, ticker="SP", issuer="0000000101", day=next_day,
             accepted=AT + timedelta(days=1), data={"ticker": "SP"})
        seed(s, ticker="NDX", issuer="0000000102", day=next_day,
             accepted=AT + timedelta(days=1), data={"ticker": "NDX"})
        _refresh_groups(s, {("0000000101", next_day), ("0000000102", next_day)})
    with session() as s, s.begin():
        assert s.get(FeedSession, sp["session_id"]).filters["index"] == "sp500"
        original = read_feed(s, session_id=sp["session_id"], index="sp500")
        assert original["groups"] == sp["groups"]
        assert feed_updates(s, sp["session_id"])["new_count"] == 1
        updated = feed_updates(s, sp["session_id"], include_groups=True)
        assert len(updated["groups"]) == 1
        assert updated["groups"][0]["issuer_id"] == "0000000101"
        target = s.get(FeedSession, updated["target_session_id"])
        assert target.filters["index"] == "sp500"
        assert read_feed(s, session_id=target.id, index="sp500")["total_groups"] == 2
        assert feed_updates(s, target.id)["new_count"] == 0
        with pytest.raises(UserError) as error:
            read_feed(s, session_id=sp["session_id"], index="nasdaq100")
        assert error.value.code == "feed.filters_changed"


def test_feed_price_keys_isolate_same_trader_hash_in_different_company_revisions():
    with session() as s, s.begin():
        first = seed(s, issuer="0000000101", ticker="ONE", price="10", data={"ticker": "ONE"})
        second = seed(s, issuer="0000000102", ticker="TWO", price="20", data={"ticker": "TWO"})
        first_view, second_view = _event_view(first), _event_view(second)
        trader_key = _trader_key(first_view)
        assert trader_key == _trader_key(second_view)
        _refresh_groups(s, {("0000000101", DAY), ("0000000102", DAY)})
        revisions = {revision.issuer_id: revision.id for revision in s.scalars(select(FeedRevision))}
    with session() as s:
        result = feed_prices(s, list(revisions.values()))["items"]
    assert set(result) == {revision + ":" + trader_key for revision in revisions.values()}
    assert Decimal(result[revisions["0000000101"] + ":" + trader_key]["price"]) == 10
    assert Decimal(result[revisions["0000000102"] + ":" + trader_key]["price"]) == 20
    assert Decimal(result[revisions["0000000101"] + ":" + trader_key]["amount"]) == 1000
    assert Decimal(result[revisions["0000000102"] + ":" + trader_key]["amount"]) == 2000


def test_invalid_filing_ticker_still_allows_overview_and_feed_prices():
    with session() as s, s.begin():
        seed(s, ticker="INVALID/SYMBOL", data={"ticker": "INVALID/SYMBOL"})
        _refresh_groups(s, {("0000000100", DAY)})
        revision = s.scalar(select(FeedRevision)).id
    assert len(read()["companies"]["items"]) == 1
    with session() as s:
        prices = feed_prices(s, [revision])["items"]
    assert len(prices) == 1
    assert Decimal(next(iter(prices.values()))["price"]) == 10
    assert next(iter(prices.values()))["current_price"] is None


@pytest.mark.parametrize("text,expected", [
    ("Transaction pursuant to a Rule 10b5-1 plan.", True),
    ("Transaction pursuant\nto a Rule\n10b5-1 trading plan.", True),
    ("Transaction was not executed pursuant to a Rule 10b5-1 plan.", False),
    ("Transaction was not executed under a Rule 10b5-1 plan.", False),
    ("No transaction was executed under Rule 10b5-1.", False),
    ("Transaction was executed without a Rule 10b5-1 plan.", False),
])
def test_plan_footnotes_recognize_newlines_and_do_not_invert_negated_evidence(text, expected):
    facts = typed_fields({"footnotes": [{"text": text}]})
    assert facts["is_plan"] is expected


@pytest.mark.parametrize("title", ["Vice  President", "Vice\nPresident", "Vice-President", "VICE PRESIDENT"])
def test_vice_president_with_whitespace_or_case_is_not_president(title):
    facts = typed_fields({}, [{"is_officer": "1", "officer_title": title}])
    assert not facts["is_president"]


def test_overview_owner_name_and_roles_come_from_filing_relationship():
    with session() as s, s.begin():
        seed(s, roles={"is_officer": "1", "officer_title": "CFO", "name": "Synthetic Officer"})
    item = read()["large_buys"]["items"][0]
    assert item["owners"][0]["name"] == "Synthetic Officer"
    assert item["owners"][0]["roles"] == ["CFO"]
