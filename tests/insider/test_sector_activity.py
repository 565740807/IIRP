"""Insider totals by saved sector and the overview's sector filter: local reads only."""

from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from iirp.analysis.calendar import ET
from iirp.api.app import app
from iirp.db import session
from iirp.insider.activity import sector_activity
from iirp.market import stock_quotes, yahoo
from iirp.models import Batch, IndexConstituent, Job, Security, SourceObservation, now
from sqlalchemy import func, select

from tests.insider.test_overview import AT, DAY, read, seed
from tests.jobs.test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401


def member(s, ticker, industry, sub_industry, cik=None):
    s.add(IndexConstituent(index_name="sp500", ticker=ticker, cik=cik, name=ticker, industry=industry,
                           sub_industry=sub_industry, classification="GICS", updated_at=now()))


def yahoo_sector(s, symbol, sector):
    s.add(Security(symbol=symbol, metadata_json={"sector": sector}))


def companies(day=DAY, accepted=AT):
    """Five companies: GICS by CIK, GICS by ticker (dot and dash), Yahoo only,
    GICS ahead of Yahoo, and none."""
    with session() as s, s.begin():
        member(s, "NVDA", "Information Technology", "Semiconductors", cik="0000000101")
        seed(s, issuer="0000000101", ticker="NVDA", day=day, accepted=accepted, price="10", shares="100", owners=("0000000201",))
        member(s, "BRK.B", "Financials", "Diversified Banks")
        seed(s, issuer="0000000102", ticker="BRK-B", day=day, accepted=accepted, code="S", price="10", shares="50", owners=("0000000202",))
        yahoo_sector(s, "ARM", "Technology")
        seed(s, issuer="0000000103", ticker="ARM", day=day, accepted=accepted, price="10", shares="30", owners=("0000000203",))
        member(s, "XOMX", "Energy", "Integrated Oil & Gas")
        yahoo_sector(s, "XOMX", "Technology")
        seed(s, issuer="0000000104", ticker="XOMX", day=day, accepted=accepted, price="10", shares="10", owners=("0000000204",))
        seed(s, issuer="0000000105", ticker="ZZZ", day=day, accepted=accepted, code="S", price="10", shares="20", owners=("0000000205",))


def test_gics_first_then_yahoo_then_unclassified_and_groups_only_from_gics():
    companies()
    with session() as s:
        result = sector_activity(s, at=AT)
    sectors = {item["id"]: item for item in result["sectors"]}
    groups = {item["id"]: item for item in result["groups"]}
    assert len(sectors) == 11 and result["companies"] == 5
    technology = sectors["information_technology"]
    assert (technology["companies"], technology["buy_amount"], technology["net_amount"]) == (2, "1300.00", "1300.00")
    # Yahoo gives a sector but never an industry group.
    assert technology["ungrouped"] == 1
    assert (groups["semiconductors"]["companies"], groups["semiconductors"]["buy_amount"]) == (1, "1000.00")
    assert groups["semiconductors"]["sector"] == "information_technology"
    assert (sectors["financials"]["sell_amount"], sectors["financials"]["net_amount"]) == ("500.00", "-500.00")
    assert groups["banks"]["sell_companies"] == 1
    # The S&P 500 list's GICS sector wins over Yahoo's.
    assert sectors["energy"]["buy_amount"] == "100.00" and sectors["energy"]["ungrouped"] == 0
    assert result["unclassified"]["companies"] == 1 and result["unclassified"]["net_amount"] == "-200.00"
    assert sectors["utilities"] == {"id": "utilities", "companies": 0, "buy_companies": 0, "sell_companies": 0,
                                    "buy_amount": None, "sell_amount": None, "net_amount": None,
                                    "amount_estimated": False, "missing_price_rows": 0,
                                    "price_mismatch_rows": 0, "ungrouped": 0}
    total = sum((Decimal(item["net_amount"]) for item in result["sectors"] if item["net_amount"]), Decimal(0))
    assert total + Decimal(result["unclassified"]["net_amount"]) == Decimal(1000 - 500 + 300 + 100 - 200)


def test_sector_totals_use_the_overview_window():
    companies(day=DAY - timedelta(days=30))
    with session() as s:
        assert sector_activity(s, at=AT)["companies"] == 0
        assert sector_activity(s, at=AT, days=31)["companies"] == 5


def tickers(section):
    return sorted(item["ticker"] for item in section["items"])


@pytest.mark.parametrize("sector,index,expected", [
    ("all", "all", ["ARM", "BRK-B", "NVDA", "XOMX", "ZZZ"]),
    ("information_technology", "all", ["ARM", "NVDA"]),
    ("information_technology", "sp500", ["NVDA"]),
    ("financials", "sp500", ["BRK-B"]),
    ("energy", "all", ["XOMX"]),
    ("unclassified", "all", ["ZZZ"]),
    ("unclassified", "sp500", []),
    ("utilities", "all", []),
])
def test_overview_sector_filter_combines_with_the_index_filter(sector, index, expected):
    companies()
    result = read(sector=sector, index=index)
    assert result["sector"] == sector and len(result["sectors"]) == 11
    assert tickers(result["companies"]) == expected
    # Every tab is filtered: the trade lists hold only the chosen sector's trades.
    trades = sorted({item["ticker"] for name in ("large_buys", "large_sales") for item in result[name]["items"]})
    assert trades == expected


def test_overview_sector_parameter_is_checked_and_reads_locally(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("sector filters must not call a provider")

    monkeypatch.setattr(yahoo, "fetch_market", forbidden)
    monkeypatch.setattr(stock_quotes, "fetch_stock_quotes", forbidden)
    companies(day=now().astimezone(ET).date(), accepted=now())
    with TestClient(app) as client:
        assert client.get("/api/v1/insider/overview", params={"sector": "nope"}).status_code == 422
        body = client.get("/api/v1/insider/overview", params={"sector": "information_technology", "index": "sp500"}).json()
        assert [item["ticker"] for item in body["companies"]["items"]] == ["NVDA"]
        sectors = client.get("/api/v1/insider/sectors").json()
        assert sectors["companies"] == 5 and len(sectors["groups"]) == 25
        assert client.get("/api/v1/insider/sectors", params={"days": 0}).status_code == 422
    with session() as s:
        assert s.scalar(select(func.count()).select_from(SourceObservation)) == 0
        assert s.scalar(select(func.count()).select_from(Job)) == 0
        assert s.scalar(select(func.count()).select_from(Batch)) == 0
