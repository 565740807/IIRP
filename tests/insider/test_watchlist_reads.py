"""Only the watched companies: the home feed (pages and new entries) and the Insider overview.

Uses a disposable iirp_v1_test_* database.
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from iirp.api.app import app
from iirp.db import session
from iirp.insider.feed import feed as read_feed
from iirp.insider.feed_updates import feed_updates
from iirp.insider.views import _refresh_groups
from iirp.market import stock_quotes, yahoo
from iirp.messages import UserError
from iirp.models import Batch, FeedSession, Job, SourceObservation, WatchlistItem
from sqlalchemy import delete, func, select

from tests.insider.test_overview import AT, DAY, read, seed
from tests.insider.test_sector_activity import companies, tickers
from tests.jobs.test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401

WATCHED, OTHER, MULTI = "0000000301", "0000000302", "0000000303"


def watch(*codes):
    with session() as s, s.begin():
        s.execute(delete(WatchlistItem))
        s.add_all(WatchlistItem(ticker=code) for code in codes)


def groups(days, issuers):
    """One group per company and day: ``days`` days back from DAY."""
    with session() as s, s.begin():
        for offset in range(days):
            day = DAY - timedelta(days=offset)
            for issuer, ticker in issuers:
                seed(s, issuer=issuer, ticker=ticker, day=day, accepted=AT - timedelta(days=offset),
                     data={"ticker": ticker})
                _refresh_groups(s, {(issuer, day)})


def all_pages(s, first, **filters):
    pages, cursor = [first], first["next_cursor"]
    while cursor:
        pages.append(read_feed(s, first["session_id"], cursor, **filters))
        cursor = pages[-1]["next_cursor"]
    return [group for page in pages for group in page["groups"]]


def test_feed_pages_keep_only_watched_companies():
    groups(25, [(WATCHED, "BRK-B"), (OTHER, "OTHR"), (MULTI, "MLT-A, MLT-B")])
    watch("BRK-B", "MLT-B")
    with session() as s, s.begin():
        everything = read_feed(s)
        first = read_feed(s, watchlist=True)
        rows = all_pages(s, first, watchlist=True)
    assert everything["total_groups"] == 75
    assert first["total_groups"] == 50 and len(first["groups"]) == 20
    # Every page is keyset-ordered and complete: no company outside the watchlist, no repeat.
    assert {group["issuer_id"] for group in rows} == {WATCHED, MULTI}
    assert len(rows) == len({group["id"] for group in rows}) == 50
    order = [(group["accepted_date"], group["id"]) for group in rows]
    assert [day for day, _ in order] == sorted((day for day, _ in order), reverse=True)


def test_feed_session_keeps_its_codes_and_new_entries_follow_the_watchlist():
    groups(3, [(WATCHED, "BRK-B"), (OTHER, "OTHR")])
    watch("BRK-B")
    with session() as s, s.begin():
        opened = read_feed(s, watchlist=True)
        assert s.get(FeedSession, opened["session_id"]).filters["watchlist"] == ["BRK-B"]
    assert opened["total_groups"] == 3
    later = DAY + timedelta(days=1)
    with session() as s, s.begin():
        for issuer, ticker in ((WATCHED, "BRK-B"), (OTHER, "OTHR")):
            seed(s, issuer=issuer, ticker=ticker, day=later, accepted=AT + timedelta(days=1), data={"ticker": ticker})
            _refresh_groups(s, {(issuer, later)})
    # The watchlist changes after the session opened: the session keeps its codes.
    watch("BRK-B", "OTHR")
    with session() as s, s.begin():
        again = read_feed(s, opened["session_id"], watchlist=True)
        assert [group["id"] for group in again["groups"]] == [group["id"] for group in opened["groups"]]
        assert feed_updates(s, opened["session_id"])["new_count"] == 1
        update = feed_updates(s, opened["session_id"], include_groups=True)
        assert [group["issuer_id"] for group in update["groups"]] == [WATCHED]
        target = s.get(FeedSession, update["target_session_id"])
        assert target.filters["watchlist"] == ["BRK-B"] and target.filters["index"] == "all"
        assert feed_updates(s, target.id)["new_count"] == 0
        with pytest.raises(UserError) as error:
            read_feed(s, opened["session_id"])
        assert error.value.code == "feed.filters_changed"
        # A new session reads the watchlist as it is now.
        assert read_feed(s, watchlist=True)["total_groups"] == 8


def test_feed_with_an_empty_watchlist_is_empty_and_combines_with_the_index():
    groups(2, [(WATCHED, "BRK-B"), (OTHER, "OTHR")])
    with session() as s, s.begin():
        assert read_feed(s, watchlist=True)["total_groups"] == 0
    watch("BRK-B", "OTHR")
    with session() as s, s.begin():
        from iirp.models import IndexConstituent

        s.add(IndexConstituent(index_name="sp500", ticker="OTHR", cik=None, name="Other", updated_at=AT))
    with session() as s, s.begin():
        both = read_feed(s, watchlist=True, index="sp500")
        assert both["total_groups"] == 2 and {group["issuer_id"] for group in both["groups"]} == {OTHER}


@pytest.mark.parametrize("codes,sector,index,expected", [
    (("NVDA", "BRK.B", "ZZZ"), "all", "all", ["BRK-B", "NVDA", "ZZZ"]),
    (("NVDA", "BRK.B", "ZZZ"), "all", "sp500", ["BRK-B", "NVDA"]),
    (("NVDA", "BRK.B", "ZZZ", "ARM"), "information_technology", "all", ["ARM", "NVDA"]),
    (("NVDA", "BRK.B", "ZZZ", "ARM"), "information_technology", "sp500", ["NVDA"]),
    (("NVDA", "BRK.B", "ZZZ"), "unclassified", "all", ["ZZZ"]),
    (("ARM",), "financials", "all", []),
    ((), "all", "all", []),
])
def test_overview_watchlist_combines_with_index_and_sector_on_every_tab(codes, sector, index, expected):
    companies()
    watch(*(code.replace(".", "-") for code in codes))
    result = read(sector=sector, index=index, watchlist=True)
    assert result["watchlist"] is True
    assert tickers(result["companies"]) == expected
    # Every tab is filtered.
    trades = sorted({item["ticker"] for name in ("large_buys", "large_sales") for item in result[name]["items"]})
    assert trades == expected
    for name in ("cluster_buys", "cluster_sales", "executive_buys", "holding_increases"):
        assert {item["ticker"] for item in result[name]["items"]} <= set(expected)
    assert read(sector=sector, index=index)["watchlist"] is False


def test_watchlist_reads_are_local(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("watchlist filters must not call a provider")

    monkeypatch.setattr(yahoo, "fetch_market", forbidden)
    monkeypatch.setattr(stock_quotes, "fetch_stock_quotes", forbidden)
    groups(2, [(WATCHED, "BRK-B"), (OTHER, "OTHR")])
    watch("BRK-B")
    with TestClient(app) as client:
        body = client.get("/api/v1/feed", params={"watchlist": "true", "type": "all"}).json()
        assert {group["issuer_id"] for group in body["groups"]} == {WATCHED}
        page = client.get("/api/v1/feed", params={"watchlist": "true", "type": "all", "session_id": body["session_id"]})
        assert page.status_code == 200
        assert client.get("/api/v1/feed", params={"type": "all", "session_id": body["session_id"]}).status_code == 409
        overview = client.get("/api/v1/insider/overview", params={"watchlist": "true", "days": 90}).json()
        assert overview["watchlist"] is True
        client.get("/api/v1/watchlist/news")
    with session() as s:
        assert s.scalar(select(func.count()).select_from(SourceObservation)) == 0
        assert s.scalar(select(func.count()).select_from(Job)) == 0
        assert s.scalar(select(func.count()).select_from(Batch)) == 0
