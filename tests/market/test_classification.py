"""GICS sub-industry → industry group, the three-step stock → sector classification,
index membership marks, and the fixed weekly index list schedule with "update now".

Uses a disposable iirp_v1_test_* database; nothing contacts SEC, Wikipedia or Yahoo.
"""

import tomllib
from datetime import datetime, timedelta, timezone

import pytest
from iirp.analysis.calendar import ET
from iirp.analysis.sectors import industry_groups, sector_map
from iirp.config import ROOT
from iirp.db import session
from iirp.market import constituents
from iirp.market.classification import (
    Memberships,
    check_gics,
    classify,
    gics,
    sub_industry_groups,
)
from iirp.market.constituents import (
    due_indices,
    last_slot,
    next_slot,
    persist_failure,
    persist_success,
)
from iirp.messages import UserError
from iirp.models import IndexConstituentState, Issuer, Job, Security
from sqlalchemy import select

from tests.jobs.test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_client,
    lifecycle_database,
)


def et(text):
    return datetime.fromisoformat(text).replace(tzinfo=ET).astimezone(timezone.utc)


# ---- GICS structure ----

def test_gics_file_has_the_2023_structure_and_matches_the_sector_map():
    config = gics()
    groups = {group["id"]: group["sector"] for group in industry_groups()}
    assert len(config["groups"]) == len(groups) == 25
    assert len(config["sectors"]) == 11
    names = [name for item in config["groups"].values() for name in item["sub_industries"]]
    assert len(names) == len(set(names)) == 163
    assert {group: item["sector"] for group, item in config["groups"].items()} == groups


@pytest.mark.parametrize(("sub_industry", "group"), [
    ("Semiconductors", "semiconductors"),
    ("Semiconductor Materials & Equipment", "semiconductors"),
    ("Application Software", "software_services"),
    ("Technology Hardware, Storage & Peripherals", "technology_hardware"),
    ("Data Processing & Outsourced Services", "commercial_professional_services"),
    ("Interactive Media & Services", "media_entertainment"),
    ("Broadline Retail", "consumer_discretionary_retail"),
    ("Consumer Staples Merchandise Retail", "consumer_staples_retail"),
    ("Transaction & Payment Processing Services", "financial_services"),
    ("Mortgage REITs", "financial_services"),
    ("Integrated Oil & Gas", "energy"),
    ("Data Center REITs", "equity_reits"),
    ("Real Estate Services", "real_estate_management_development"),
    ("Passenger Airlines", "transportation"),
    # Names before the 2023 revision still classify.
    ("Internet & Direct Marketing Retail", "consumer_discretionary_retail"),
    ("Hypermarkets & Super Centers", "consumer_staples_retail"),
    ("Airlines", "transportation"),
])
def test_sub_industries_map_to_their_industry_group(sub_industry, group):
    assert sub_industry_groups()[sub_industry] == group


def test_gics_check_rejects_a_wrong_sector_or_a_repeated_sub_industry():
    with (ROOT / "config/gics.toml").open("rb") as source:
        config = tomllib.load(source)
    groups = {group["id"]: group["sector"] for group in industry_groups()}
    wrong = {**config, "groups": {**config["groups"], "banks": {**config["groups"]["banks"], "sector": "energy"}}}
    with pytest.raises(ValueError, match="banks belongs to financials"):
        check_gics(wrong, groups)
    repeated = {**config, "groups": {**config["groups"], "insurance": {
        "sector": "financials", "sub_industries": ["Reinsurance", "Regional Banks"]}}}
    with pytest.raises(ValueError, match="Regional Banks"):
        check_gics(repeated, groups)


def test_yahoo_sector_names_map_to_the_eleven_sectors():
    assert sector_map()["yahoo_sectors"] == {
        "Technology": "information_technology", "Healthcare": "health_care",
        "Financial Services": "financials", "Consumer Cyclical": "consumer_discretionary",
        "Consumer Defensive": "consumer_staples", "Communication Services": "communication_services",
        "Industrials": "industrials", "Energy": "energy", "Basic Materials": "materials",
        "Utilities": "utilities", "Real Estate": "real_estate"}


# ---- Classification and membership (database) ----

AT = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
SP500 = [
    {"index_name": "sp500", "ticker": "NVDA", "cik": "0001045810", "name": "Nvidia", "industry": "Information Technology",
     "sub_industry": "Semiconductors", "classification": "GICS"},
    {"index_name": "sp500", "ticker": "AAPL", "cik": "0000320193", "name": "Apple", "industry": "Information Technology",
     "sub_industry": "Technology Hardware, Storage & Peripherals", "classification": "GICS"},
    {"index_name": "sp500", "ticker": "XOM", "cik": "0000034088", "name": "Exxon Mobil", "industry": "Energy",
     "sub_industry": "Integrated Oil & Gas", "classification": "GICS"},
    {"index_name": "sp500", "ticker": "BRK.B", "cik": "0001067983", "name": "Berkshire Hathaway",
     "industry": "Financials", "sub_industry": "Multi-Sector Holdings", "classification": "GICS"},
]
NASDAQ100 = [
    {"index_name": "nasdaq100", "ticker": "NVDA", "cik": None, "name": "Nvidia", "industry": "Technology",
     "sub_industry": "Semiconductors", "classification": "ICB"},
    {"index_name": "nasdaq100", "ticker": "ARM", "cik": None, "name": "Arm Holdings", "industry": "Technology",
     "sub_industry": "Semiconductors", "classification": "ICB"},
]


@pytest.fixture
def lists():
    with session() as s, s.begin():
        persist_success(s, "sp500", SP500, at=AT)
        persist_success(s, "nasdaq100", NASDAQ100, at=AT)


def security(symbol, **metadata):
    with session() as s, s.begin():
        s.add(Security(symbol=symbol, status="VERIFIED", instrument="EQUITY", metadata_json=metadata))


def test_sp500_gics_comes_first_with_group_etfs(lists):
    security("NVDA", sector="Healthcare", industry="Wrong on purpose")
    with session() as s:
        nvda = classify(s, "nvda")
        apple = classify(s, "AAPL")
        exxon = classify(s, "XOM")
        berkshire = classify(s, "BRK-B")
    assert nvda["source"] == "sp500_gics"
    assert nvda["sector"] == {"id": "information_technology", "etf": "XLK"}
    assert nvda["industry_group"] == {"id": "semiconductors", "etf": "SOXX", "coverage": "partial"}
    assert nvda["indexes"] == ["sp500", "nasdaq100"]
    # A group without a suitable ETF has none; an energy stock's group is the sector itself.
    assert apple["industry_group"] == {"id": "technology_hardware", "etf": None, "coverage": "pending"}
    assert (exxon["industry_group"]["id"], exxon["industry_group"]["etf"]) == ("energy", "XLE")
    assert berkshire["sector"]["etf"] == "XLF" and berkshire["gics_sub_industry"] == "Multi-Sector Holdings"
    assert berkshire["indexes"] == ["sp500"]


def test_yahoo_sector_when_not_in_the_sp500_list(lists):
    security("ARM", sector="Technology", industry="Semiconductors")
    security("SHOP", sector="Consumer Cyclical", industry="Internet Retail")
    with session() as s:
        arm = classify(s, "ARM")
        shop = classify(s, "SHOP")
    # The Nasdaq-100 list's ICB industry is kept but not used.
    assert (arm["source"], arm["sector"], arm["industry_group"]) == (
        "yahoo", {"id": "information_technology", "etf": "XLK"}, None)
    assert arm["indexes"] == ["nasdaq100"]
    assert (shop["source"], shop["sector"]["id"], shop["yahoo_industry"]) == (
        "yahoo", "consumer_discretionary", "Internet Retail")
    assert shop["refresh"] == "none"


def test_no_sector_asks_once_for_an_identity_refresh_only_when_needed(lists, lifecycle_client):  # noqa: F811
    security("OLDCO", name_only=True)            # identity checked before sectors were kept
    security("SPY", sector=None, industry=None)  # checked: Yahoo has no sector for an ETF
    with session() as s:
        assert (classify(s, "SPY")["source"], classify(s, "SPY")["refresh"]) == ("none", "none")
    first = lifecycle_client.get("/api/v1/securities/OLDCO/classification").json()
    assert (first["source"], first["refresh"]) == ("none", "needed")
    asked = lifecycle_client.post("/api/v1/securities/OLDCO/classification/refresh")
    assert asked.status_code == 202 and asked.json()["refresh"] == "pending"
    again = lifecycle_client.post("/api/v1/securities/OLDCO/classification/refresh").json()
    assert again["refresh"] == "pending"
    # A symbol with nothing saved yet gets a pending security and one identity job too.
    assert lifecycle_client.post("/api/v1/securities/NEWCO/classification/refresh").json()["refresh"] == "pending"
    # Nothing is requested for stocks that already have an answer.
    assert lifecycle_client.post("/api/v1/securities/SPY/classification/refresh").json()["refresh"] == "none"
    assert lifecycle_client.post("/api/v1/securities/NVDA/classification/refresh").json()["source"] == "sp500_gics"
    with session() as s:
        jobs = s.scalars(select(Job.target["symbol"].astext).where(Job.kind == "market_identity")).all()
    assert sorted(jobs) == ["NEWCO", "OLDCO"]
    assert lifecycle_client.get("/api/v1/securities/bad%20symbol/classification").status_code == 422


def test_index_marks_use_cik_or_ticker(lists, lifecycle_client):  # noqa: F811
    with session() as s, s.begin():
        s.add(Issuer(id="0001067983", name="Berkshire Hathaway Inc", ticker="BRK-B"))
        s.add(Issuer(id="0001730168", name="Broadcom Inc", ticker="AVGO"))
    with session() as s:
        members = Memberships(s)
        # Wikipedia gives no CIK; the Nasdaq-100 row takes it from the S&P 500 list on saving.
        assert members.of("0001045810") == ["sp500", "nasdaq100"]
        assert members.of("0000320193") == ["sp500"]
        assert members.of(None, "nvda") == ["sp500", "nasdaq100"]
        assert members.of("0001067983", "BRK-B") == ["sp500"]
        assert members.of("0000000001", "ZZZ") == []
        rows = members.mark([{"issuer_id": "0000320193", "ticker": None}, {"issuer_id": "x", "ticker": "ARM"}])
        assert [row["indexes"] for row in rows] == [["sp500"], ["nasdaq100"]]
    found = lifecycle_client.get("/api/v1/insider/lookup", params={"q": "Berkshire"}).json()
    assert found["companies"][0]["indexes"] == ["sp500"]


# ---- Weekly schedule and "update now" ----

@pytest.mark.parametrize(("at", "last", "following"), [
    ("2026-10-10T07:59:00", "2026-10-03T08:00:00", "2026-10-10T08:00:00"),
    ("2026-10-10T08:00:00", "2026-10-10T08:00:00", "2026-10-17T08:00:00"),
    ("2026-10-14T15:00:00", "2026-10-10T08:00:00", "2026-10-17T08:00:00"),
    # 08:00 Eastern follows daylight saving time: 13:00 UTC on Nov 7, 12:00 UTC on Nov 14.
    ("2026-11-09T09:00:00", "2026-11-07T08:00:00", "2026-11-14T08:00:00"),
])
def test_the_weekly_time_is_saturday_eight_eastern(at, last, following):
    assert last_slot(et(at)) == et(last)
    assert next_slot(et(at)) == et(following)
    assert next_slot(et(at)).astimezone(ET).strftime("%A %H:%M") == "Saturday 08:00"


def test_due_once_per_week_and_a_failure_retries_with_the_last_list_kept(lists):
    saturday = et("2026-10-17T08:00:00")
    with session() as s:
        assert due_indices(s, at=saturday - timedelta(minutes=1)) == []
        assert due_indices(s, at=saturday) == ["sp500", "nasdaq100"]
    with session() as s, s.begin():
        persist_success(s, "sp500", SP500, at=saturday + timedelta(minutes=1))
        persist_failure(s, "nasdaq100", UserError("index.download_failed", index="nasdaq100", reason="http_503"),
                        at=saturday + timedelta(minutes=1))
    with session() as s:
        assert due_indices(s, at=saturday + timedelta(hours=1)) == []
        assert due_indices(s, at=saturday + timedelta(hours=6, minutes=1)) == ["nasdaq100"]
        state = s.get(IndexConstituentState, "nasdaq100")
        assert state.updated_at == AT
        result = constituents.status(s, at=saturday + timedelta(hours=1))
    items = {item["index_name"]: item for item in result["items"]}
    assert items["nasdaq100"]["members"] == 2 and items["nasdaq100"]["error"]
    assert items["nasdaq100"]["next_run_at"] == saturday + timedelta(hours=6, minutes=1)
    assert items["sp500"]["next_run_at"] == et("2026-10-24T08:00:00")
    assert (result["weekday"], result["time"], result["retry_seconds"]) == ("saturday", "08:00", 21600)


def test_update_now_queues_one_job_per_list(lifecycle_client):  # noqa: F811
    state = lifecycle_client.get("/api/v1/index-constituents").json()
    assert [item["index_name"] for item in state["items"]] == ["sp500", "nasdaq100"]
    assert all(item["members"] == 0 and not item["running"] for item in state["items"])
    started = lifecycle_client.post("/api/v1/index-constituents/refresh")
    assert started.status_code == 202
    assert all(item["running"] for item in started.json()["items"])
    lifecycle_client.post("/api/v1/index-constituents/refresh")
    with session() as s:
        jobs = s.scalars(select(Job).where(Job.kind == "index_constituents")).all()
    assert sorted((job.target["index_name"], job.trigger) for job in jobs) == [
        ("nasdaq100", "manual"), ("sp500", "manual")]


def test_identity_check_keeps_yahoo_sector_and_industry_from_the_same_request(monkeypatch, tmp_path):
    import yfinance
    from iirp.market.yahoo import fetch_market

    calls = []

    class Ticker:
        def __init__(self, symbol, session=None):
            self.symbol = symbol

        def get_info(self):
            calls.append(self.symbol)
            return {"symbol": "SHOP", "quoteType": "EQUITY", "currency": "USD", "exchange": "NYQ",
                    "longName": "Shopify Inc.", "sector": "Consumer Cyclical", "industry": "Internet Retail"}

    monkeypatch.setattr(yfinance, "Ticker", Ticker)
    response = fetch_market("market_identity", {"symbol": "SHOP"})
    assert calls == ["SHOP"]
    assert (response["metadata"]["sector"], response["metadata"]["industry"]) == ("Consumer Cyclical", "Internet Retail")
