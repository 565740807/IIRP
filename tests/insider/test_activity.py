"""Open-market Insider activity per stock code: the overview's rules, local reads only."""

from datetime import timedelta

from fastapi.testclient import TestClient
from iirp.analysis.calendar import ET
from iirp.api.app import app
from iirp.db import session
from iirp.insider.activity import activity
from iirp.market import stock_quotes, yahoo
from iirp.models import Batch, Job, SourceObservation, now
from sqlalchemy import func, select

from tests.insider.test_overview import AT, DAY, seed
from tests.insider.test_overview_prices import bar
from tests.jobs.test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401


def read(*tickers, days=30):
    with session() as s:
        return activity(s, tickers, days=days, at=AT)


def test_only_open_market_purchases_and_sales_count():
    with session() as s, s.begin():
        seed(s, code="P", price="10", shares="100", owners=("0000000200",))
        seed(s, code="S", price="20", shares="30", owners=("0000000201",))
        seed(s, code="A", price="0", shares="500", owners=("0000000202",))
        seed(s, code="M", price="5", shares="50", owners=("0000000203",))
        seed(s, code="P", price="10", shares="100", owners=("0000000204",), data={"table": "II"})
        seed(s, code="P", price="10", shares="100", owners=("0000000205",), data={"currency": "EUR"})
        seed(s, code="P", price="10", shares="100", owners=("0000000206",), status="SUPERSEDED")
        seed(s, code="P", price="10", shares="100", owners=("0000000207",), visible=False)
    item = read("TEST")["items"]["TEST"]
    assert item["buy"] == {"people": 1, "trades": 1, "amount": "1000.00"}
    assert item["sell"] == {"people": 1, "trades": 1, "amount": "600.00"}
    assert item["net_amount"] == "400.00"
    assert item["trader_count"] == 2
    assert [(trader["id"], trader["side"], trader["amount"]) for trader in item["traders"]] == [
        ("0000000200", "buy", "1000.00"), ("0000000201", "sell", "600.00")]


def test_window_is_trade_dates_over_the_last_calendar_days_ending_today():
    first = DAY - timedelta(days=29)
    with session() as s, s.begin():
        seed(s, day=first, owners=("0000000200",))
        seed(s, day=first - timedelta(days=1), owners=("0000000201",))
        # Dated after its own acceptance day: never counted, as in the overview.
        seed(s, day=DAY + timedelta(days=1), owners=("0000000202",))
    result = read("TEST")
    assert (result["start_date"], result["end_date"]) == (first.isoformat(), DAY.isoformat())
    item = result["items"]["TEST"]
    assert item["buy"]["trades"] == 1 and item["last_date"] == first.isoformat()
    assert read("TEST", days=7)["items"] == {}


def test_net_plans_people_and_unknown_amounts():
    with session() as s, s.begin():
        seed(s, code="P", price="10", shares="100", owners=("0000000200", "0000000201"))
        seed(s, code="P", price="10", shares="50", owners=("0000000200",), plan=True)
        seed(s, code="S", price="10", shares="400", owners=("0000000202",), plan=True)
    item = read("TEST")["items"]["TEST"]
    # A joint filing names each owner; each counts once per side.
    assert item["buy"] == {"people": 2, "trades": 2, "amount": "1500.00"}
    assert item["sell"] == {"people": 1, "trades": 1, "amount": "4000.00"}
    assert item["net_amount"] == "-2500.00"
    assert item["plan_trades"] == 2
    with session() as s, s.begin():
        # A sale without any price or daily bar: that side, and with it the net, is unknown.
        seed(s, code="S", price=None, shares="10", owners=("0000000203",))
        seed(s, issuer="0000000101", ticker="ONLY", code="S", price=None, shares="10")
    item = read("TEST")["items"]["TEST"]
    assert item["sell"]["amount"] == "4000.00" and item["missing_price_rows"] == 1
    only = read("ONLY")["items"]["ONLY"]
    assert only["sell"] == {"people": 1, "trades": 1, "amount": None}
    assert only["net_amount"] is None


def test_suspect_prices_count_as_trades_but_not_in_amounts():
    with session() as s, s.begin():
        seed(s, price="1000", shares="10", owners=("0000000200",))
        seed(s, price="10", shares="10", owners=("0000000201",))
        bar(s, "TEST", 10)
    item = read("TEST")["items"]["TEST"]
    assert item["buy"] == {"people": 2, "trades": 2, "amount": "100.00"}
    assert item["price_mismatch_rows"] == 1


def test_dot_and_dash_are_the_same_and_any_stated_class_matches():
    with session() as s, s.begin():
        seed(s, issuer="0000000101", ticker="BRK.B", owners=("0000000200",))
        seed(s, issuer="0000000102", ticker=None, owners=("0000000201",), data={"ticker": "LEN, LEN.B"})
        seed(s, issuer="0000000103", ticker="OTHER", owners=("0000000202",))
    result = read("brk-b", "BRK.B", "LEN.B", "len", "NONE")
    assert sorted(result["items"]) == ["BRK-B", "LEN", "LEN-B"]
    assert result["items"]["BRK-B"]["issuer_id"] == "0000000101"
    assert result["items"]["BRK-B"]["ticker"] == "BRK.B"
    assert result["items"]["LEN-B"]["ticker"] == "LEN"
    assert result["items"]["LEN"]["buy"]["trades"] == 1


def test_three_largest_traders_with_their_roles():
    with session() as s, s.begin():
        for index, shares in enumerate(("10", "40", "20", "30")):
            seed(s, shares=shares, owners=(f"000000020{index}",),
                 roles={"is_officer": "1", "officer_title": "CFO"} if index == 1 else None)
    item = read("TEST")["items"]["TEST"]
    assert item["trader_count"] == 4
    assert [trader["amount"] for trader in item["traders"]] == ["400.00", "300.00", "200.00"]
    assert item["traders"][0]["roles"] == ["CFO"]
    assert item["traders"][0]["name"] == "Owner 0000000201"


def test_endpoint_reads_locally_and_limits_the_batch(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("activity marks must not call a provider")

    monkeypatch.setattr(yahoo, "fetch_market", forbidden)
    monkeypatch.setattr(stock_quotes, "fetch_stock_quotes", forbidden)
    today = now().astimezone(ET).date()
    with session() as s, s.begin():
        seed(s, day=today, accepted=now())
    with TestClient(app) as client:
        body = client.get("/api/v1/insider/activity", params={"tickers": "TEST,AAPL"}).json()
        assert list(body["items"]) == ["TEST"] and body["days"] == 30
        assert client.get("/api/v1/insider/activity").json()["items"] == {}
        many = ",".join(f"T{index}" for index in range(201))
        assert client.get("/api/v1/insider/activity", params={"tickers": many}).status_code == 422
        assert client.get("/api/v1/insider/activity", params={"tickers": "A B"}).status_code == 422
        assert client.get("/api/v1/insider/activity", params={"tickers": "TEST", "days": 91}).status_code == 422
    with session() as s:
        assert s.scalar(select(func.count()).select_from(SourceObservation)) == 0
        assert s.scalar(select(func.count()).select_from(Job)) == 0
        assert s.scalar(select(func.count()).select_from(Batch)) == 0


def test_no_codes_reads_nothing():
    assert read()["items"] == {}
