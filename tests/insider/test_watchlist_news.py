"""News of watched codes on the home page: new open-market Insider trades by SEC acceptance time.

Uses a disposable iirp_v1_test_* database.
"""

from datetime import timedelta

from fastapi.testclient import TestClient
from iirp.analysis.calendar import ET
from iirp.api.app import app
from iirp.db import session
from iirp.insider.activity import new_trades
from iirp.market import stock_quotes, yahoo
from iirp.models import Batch, Job, SourceObservation, WatchlistItem, now
from iirp.watchlist import acknowledge, since
from sqlalchemy import func, select

from tests.insider.test_overview import AT, seed
from tests.jobs.test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401

NVDA, AAPL = "0000000401", "0000000402"


def trade(accepted, *, issuer=NVDA, ticker="NVDA", code="P", **fields):
    with session() as s, s.begin():
        return seed(s, issuer=issuer, ticker=ticker, code=code, accepted=accepted,
                    day=accepted.astimezone(ET).date(), **fields).id


def watch(code, added, acknowledged=None):
    with session() as s, s.begin():
        s.add(WatchlistItem(ticker=code, added_at=added))
        if acknowledged:
            acknowledge(s, acknowledged)


def news(at=AT):
    with session() as s:
        return {item["ticker"]: item for item in new_trades(s, since(s), at=at)}


def test_only_trades_accepted_after_the_last_acknowledgement_are_new():
    watch("NVDA", AT - timedelta(days=20), acknowledged=AT - timedelta(days=3))
    trade(AT - timedelta(days=5), owners=("0000000501",))
    newest = trade(AT - timedelta(days=1), code="S", price="20", shares="10", owners=("0000000502",))
    trade(AT - timedelta(hours=2), owners=("0000000503",), data={"table": "II"})  # Not open-market Table I.
    trade(AT - timedelta(hours=2), code="A", owners=("0000000504",))
    item = news()["NVDA"]
    assert (item["trades"], item["buys"], item["sales"]) == (1, 0, 1)
    assert item["issuer_id"] == NVDA and item["since"] == (AT - timedelta(days=3)).isoformat()
    [row] = item["traders"]
    assert (row["id"], row["side"], row["amount"], row["trades"]) == (newest, "sell", "200.00", 1)
    assert row["owners"] == [{"id": "0000000502", "name": "Owner 0000000502"}]
    # Once acknowledged, nothing is new; a later trade is.
    with session() as s, s.begin():
        acknowledge(s, AT)
    assert news() == {}
    later = trade(AT + timedelta(hours=1), owners=("0000000505",))
    assert [row["id"] for row in news(AT + timedelta(hours=2))["NVDA"]["traders"]] == [later]


def test_a_newly_watched_code_starts_when_it_was_added():
    watch("NVDA", AT - timedelta(days=1), acknowledged=AT - timedelta(days=7))
    trade(AT - timedelta(days=2), owners=("0000000501",))
    fresh = trade(AT - timedelta(hours=3), owners=("0000000502",))
    item = news()["NVDA"]
    assert [row["id"] for row in item["traders"]] == [fresh]
    assert item["since"] == (AT - timedelta(days=1)).isoformat()


def test_a_correction_is_not_news_a_second_time():
    watch("NVDA", AT - timedelta(days=20), acknowledged=AT - timedelta(days=3))
    # Filed before the acknowledgement, corrected after it: already seen.
    old = AT - timedelta(days=5)
    trade(old, owners=("0000000501",), status="SUPERSEDED")
    trade(AT - timedelta(days=1), owners=("0000000501",), data={"original_accepted_at": old.isoformat()})
    # Filed after it and corrected: one new trade, not two.
    first = AT - timedelta(days=2)
    trade(first, owners=("0000000502",), status="SUPERSEDED")
    corrected = trade(AT - timedelta(hours=5), owners=("0000000502",),
                      data={"original_accepted_at": first.isoformat()})
    item = news()["NVDA"]
    assert item["trades"] == 1 and item["traders"][0]["id"] == corrected
    assert item["traders"][0]["accepted_at"] == first.isoformat()


def test_one_line_per_insider_and_side_with_the_trades_counted_one_by_one():
    watch("NVDA", AT - timedelta(days=20), acknowledged=AT - timedelta(days=10))
    day = AT - timedelta(days=2)
    # The same insider sells four times on one day: one line, four trades.
    same_day = [trade(day + timedelta(minutes=minute), code="S", price="10", shares="100", owners=("0000000501",))
                for minute in range(4)]
    # Another insider buys on three different days: one line with the range of days.
    buys = [trade(AT - timedelta(days=offset), price="20", shares="10", owners=("0000000502",)) for offset in (5, 4, 1)]
    # The same insider also sells: a line of its own.
    trade(AT - timedelta(days=3), code="S", price="20", shares="1", owners=("0000000502",))
    item = news()["NVDA"]
    # The notice still counts transactions one by one.
    assert (item["trades"], item["buys"], item["sales"], item["trader_count"]) == (8, 3, 5, 3)
    lines = item["traders"]
    assert [(line["owners"][0]["id"], line["side"], line["trades"], line["amount"]) for line in lines] == [
        ("0000000501", "sell", 4, "4000.00"), ("0000000502", "buy", 3, "600.00"), ("0000000502", "sell", 1, "20.00")]
    four, three = lines[0], lines[1]
    assert four["first_date"] == four["last_date"] == day.astimezone(ET).date().isoformat()
    # Each line opens the insider's newest trade on that side.
    assert four["id"] == same_day[-1] and three["id"] == buys[-1]
    assert (three["first_date"], three["last_date"]) == (
        (AT - timedelta(days=5)).astimezone(ET).date().isoformat(), (AT - timedelta(days=1)).astimezone(ET).date().isoformat())


def test_largest_amount_first_unknown_last_and_at_most_five_lines():
    watch("NVDA", AT - timedelta(days=20), acknowledged=AT - timedelta(days=10))
    # No price and no daily bar: the amount is unknown, so the line comes last.
    trade(AT - timedelta(days=1), price=None, shares="1000000", owners=("0000000699",))
    trade(AT - timedelta(days=1), price="10", shares="1", owners=("0000000601",))
    trade(AT - timedelta(days=1), price="10", shares="500", owners=("0000000602",))
    lines = news()["NVDA"]["traders"]
    assert [(line["owners"][0]["id"], line["amount"]) for line in lines] == [
        ("0000000602", "5000.00"), ("0000000601", "10.00"), ("0000000699", None)]
    for index, shares in enumerate(("30", "7", "90", "2000")):
        trade(AT - timedelta(days=1), price="10", shares=shares, owners=(f"00000007{index:02d}",))
    item = news()["NVDA"]
    # Seven insiders: five lines are returned, the rest are only counted ("n more").
    assert item["trader_count"] == 7 and item["trades"] == 7
    assert [line["amount"] for line in item["traders"]] == ["20000.00", "5000.00", "900.00", "300.00", "70.00"]


def test_codes_match_any_stated_class_and_newest_first():
    watch("BRK-B", AT - timedelta(days=10))
    watch("NVDA", AT - timedelta(days=10))
    trade(AT - timedelta(days=2), issuer=AAPL, ticker="BRK.A, BRK.B", owners=("0000000501",))
    trade(AT - timedelta(days=1), owners=("0000000502",))
    trade(AT - timedelta(days=1), issuer="0000000403", ticker="OTHER", owners=("0000000503",))
    with session() as s:
        result = new_trades(s, since(s), at=AT)
    assert [item["ticker"] for item in result] == ["NVDA", "BRK-B"]
    # Nothing older than thirty days is read, however long ago the code was added.
    with session() as s:
        assert new_trades(s, {"NVDA": AT - timedelta(days=90)}, at=AT + timedelta(days=31)) == []


def test_endpoint_reads_locally_and_acknowledging_clears_it(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("watchlist news must not call a provider")

    monkeypatch.setattr(yahoo, "fetch_market", forbidden)
    monkeypatch.setattr(stock_quotes, "fetch_stock_quotes", forbidden)
    current = now()
    watch("NVDA", current - timedelta(days=5))
    trade(current - timedelta(hours=1), owners=("0000000501",))
    with TestClient(app, headers={"X-IIRP-Client": "web"}) as client:
        body = client.get("/api/v1/watchlist/news").json()
        assert [item["ticker"] for item in body["insider"]] == ["NVDA"] and body["acknowledged_at"] is None
        assert set(body) == {"as_of", "acknowledged_at", "insider"}
        client.put("/api/v1/watchlist/acknowledged", json={"at": body["as_of"]})
        assert client.get("/api/v1/watchlist/news").json()["insider"] == []
    with session() as s:
        assert s.scalar(select(func.count()).select_from(SourceObservation)) == 0
        assert s.scalar(select(func.count()).select_from(Job)) == 0
        assert s.scalar(select(func.count()).select_from(Batch)) == 0
