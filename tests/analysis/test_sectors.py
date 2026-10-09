"""Sector performance: period anchors, listing dates, heatmap weights, candles and reads.

The hand-checked samples use XLK and XLV daily bars from Yahoo (split-only
adjusted, as the 24-hour cache stores them, rounded to the cache's 12
decimals) in data/sector_etf_sample.csv: 2025-09-02 through 2025-11-03.
"""

import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from iirp.analysis.calendar import ET, sessions
from iirp.analysis.research import compute_research
from iirp.analysis.sectors import (
    aggregate,
    candles,
    closing_periods,
    full_years,
    heatmap_weights,
    months_back,
    performance,
    request_prices,
    sector_etfs,
    sector_map,
    today_change,
)
from iirp.db import session
from iirp.market import stock_quotes, yahoo
from iirp.market.stock_quotes import KIND, request_stock_quotes
from iirp.models import Batch, Job, MarketQuote, PriceCache, PriceCacheBar, Security, now
from sqlalchemy import func, select

from tests.jobs.test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401

SAMPLE = Path(__file__).with_name("data") / "sector_etf_sample.csv"


def sample(symbol):
    with SAMPLE.open() as handle:
        return [{**row, "status": "VALID"} for row in csv.DictReader(handle) if row["symbol"] == symbol]


def closes(bars):
    return {date.fromisoformat(bar["date"]): Decimal(bar["close"]) for bar in bars}


def flat(start, end, close="100"):
    return {day: Decimal(close) for day in sessions(start, end)}


def et(text):
    return datetime.fromisoformat(text).replace(tzinfo=ET)


def save_cache(s, symbol, bars, *, start=None):
    security = Security(symbol=symbol, instrument="ETF", calendar="XNYS", status="VERIFIED")
    s.add(security)
    s.flush()
    days = [date.fromisoformat(bar["date"]) for bar in bars]
    cache = PriceCache(security_id=security.id, start_date=start or min(days), end_date=max(days),
                       complete_through=max(days), fetched_at=now(),
                       expires_at=now() + timedelta(hours=24), details={})
    s.add(cache)
    s.flush()
    for bar in bars:
        s.add(PriceCacheBar(cache_id=cache.id, session_date=date.fromisoformat(bar["date"]),
                            open=bar["open"], high=bar["high"], low=bar["low"], close=bar["close"],
                            status="VALID"))


# --- configuration -----------------------------------------------------------

def test_sector_map_lists_the_eleven_spdr_sector_etfs():
    assert sector_etfs() == ["XLK", "XLV", "XLF", "XLY", "XLP", "XLC", "XLI", "XLE", "XLB",
                             "XLU", "XLRE"]
    assert sector_map()["history"]["years"] == 8
    assert all(not item.get("group") for item in sector_map()["sector"])


# --- period anchors ----------------------------------------------------------

@pytest.mark.parametrize("day,months,expected", [
    (date(2025, 3, 31), 1, date(2025, 2, 28)),
    (date(2024, 3, 31), 1, date(2024, 2, 29)),
    (date(2025, 5, 30), 3, date(2025, 2, 28)),
    (date(2026, 1, 15), 1, date(2025, 12, 15)),
    (date(2025, 12, 31), 3, date(2025, 9, 30)),
])
def test_months_back_keeps_the_day_or_takes_the_month_end(day, months, expected):
    assert months_back(day, months) == expected


def starts(last, data=None):
    data = data or flat(last - timedelta(days=400), last)
    return {period: value["start_date"] for period, value in closing_periods(
        data, min(data), last, min(data)).items()}


def test_week_anchor_on_a_holiday_moves_to_the_session_before():
    # 2025-07-11 − 7 days = 2025-07-04, Independence Day → Thursday 07-03.
    assert starts(date(2025, 7, 11))["1w"] == "2025-07-03"


def test_month_end_anchors():
    # 2025-03-31 − 1 month = 2025-02-28 (Friday); − 3 months = 2024-12-31.
    result = starts(date(2025, 3, 31))
    assert (result["1m"], result["3m"]) == ("2025-02-28", "2024-12-31")
    # 2025-11-03 − 3 months = Sunday 2025-08-03 → Friday 08-01; − 1 month = Friday 10-03.
    result = starts(date(2025, 11, 3))
    assert (result["1m"], result["3m"]) == ("2025-10-03", "2025-08-01")


def test_year_start_compares_with_the_previous_years_last_session():
    result = starts(date(2026, 1, 2))
    assert result["ytd"] == "2025-12-31"
    # 2025-01-02 − 7 days = 2024-12-26, a session.
    assert starts(date(2025, 1, 2))["1w"] == "2024-12-26"
    assert starts(date(2025, 1, 2))["ytd"] == "2024-12-31"


def test_values_are_close_to_close_with_their_dates():
    data = flat(date(2025, 1, 1), date(2025, 3, 31))
    data[date(2025, 2, 28)] = Decimal(80)
    result = closing_periods(data, min(data), date(2025, 3, 31), min(data))
    assert result["1m"] == {"value": "0.250000", "start_date": "2025-02-28",
                            "end_date": "2025-03-31", "status": "available"}


def test_xlc_has_no_value_before_its_first_session():
    # XLC started trading on 2018-06-19; the fetch asked for dates from 2017-12-01.
    data = flat(date(2018, 6, 19), date(2018, 8, 31))
    result = closing_periods(data, date(2018, 6, 19), date(2018, 8, 31), date(2017, 12, 1))
    assert result["1m"]["status"] == "available"
    assert result["3m"] == {"value": None, "start_date": "2018-05-31", "end_date": "2018-08-31",
                            "status": "before_listing"}
    assert result["ytd"]["status"] == "before_listing"
    # A narrower fetch that started later does not prove the ETF was not trading.
    narrow = closing_periods(data, date(2018, 6, 19), date(2018, 8, 31), date(2018, 6, 19))
    assert narrow["3m"]["status"] == "missing"


def test_full_years_count_only_years_traded_from_january():
    assert full_years(date(2018, 6, 19), date(2026, 10, 9), 8) == 7   # XLC: 2019–2025
    assert full_years(date(2015, 10, 8), date(2026, 10, 9), 8) == 8   # XLRE
    assert full_years(date(2017, 12, 1), date(2026, 10, 9), 8) == 8
    assert full_years(None, date(2026, 10, 9), 8) == 0


# --- hand-checked samples ----------------------------------------------------

@pytest.mark.parametrize("symbol,start_close,end_close,expected", [
    # 150.339996337891 / 140.929992675781 − 1 = +6.6771%
    ("XLK", "140.929992675781", "150.339996337891", "0.066771"),
    # 144.25 / 139.169998168945 − 1 = +3.6502%
    ("XLV", "139.169998168945", "144.250000000000", "0.036502"),
])
def test_one_month_by_hand(symbol, start_close, end_close, expected):
    bars = closes(sample(symbol))
    # Last completed session 2025-10-31; 10-31 − 1 month = 2025-09-30, a session.
    result = closing_periods(bars, min(bars), date(2025, 10, 31), min(bars))["1m"]
    assert (result["start_date"], result["end_date"]) == ("2025-09-30", "2025-10-31")
    assert bars[date(2025, 9, 30)] == Decimal(start_close)
    assert bars[date(2025, 10, 31)] == Decimal(end_close)
    assert result["value"] == expected
    assert Decimal(result["value"]) == round(Decimal(end_close) / Decimal(start_close) - 1, 6)


@pytest.mark.parametrize("symbol,opening,closing,high,low,change", [
    # Oct 1 open → Oct 31 close: 150.339996337891 / 140.210006713867 − 1 = +7.2249%
    ("XLK", "140.210006713867", "150.339996337891", "152.994995117188", "139.119995117188",
     "0.07224869"),
    # 144.25 / 139.800003051758 − 1 = +3.1831%
    ("XLV", "139.800003051758", "144.250000000000", "146.759994506836", "139.600006103516",
     "0.03183116"),
])
def test_october_2025_open_to_close_by_hand(symbol, opening, closing, high, low, change):
    result = compute_research({"kind": "monthly", "historical_years": 1, "current_year": 2026},
                              sample(symbol), today=date(2026, 10, 6))
    october = next(item for item in result["periods"] if item["key"] == "10")
    candle = next(item for item in october["years"] if item["year"] == 2025)
    assert (candle["start_date"], candle["end_date"]) == ("2025-10-01", "2025-10-31")
    assert (Decimal(candle["open"]), Decimal(candle["close"])) == (Decimal(opening), Decimal(closing))
    assert (Decimal(candle["high"]), Decimal(candle["low"])) == (Decimal(high), Decimal(low))
    assert candle["sessions"] == 23
    assert round(Decimal(candle["change"]), 8) == Decimal(change)


def test_sector_read_matches_the_hand_samples():
    with session() as s, s.begin():
        for symbol in ("XLK", "XLV"):
            save_cache(s, symbol, sample(symbol), start=date(2025, 8, 1))
    # Saturday 2025-11-01: the last completed session is Friday 10-31.
    with session() as s:
        result = performance(s, at=et("2025-11-01T12:00:00"))
    items = {item["etf"]: item for item in result["items"]}
    assert items["XLK"]["periods"]["1m"]["value"] == "0.066771"
    assert items["XLV"]["periods"]["1m"]["value"] == "0.036502"
    # 1 week: 10-24 close 146.789993286133 → 150.339996337891.
    assert items["XLK"]["periods"]["1w"]["start_date"] == "2025-10-24"
    assert items["XLK"]["periods"]["1w"]["value"] == "0.024184"
    # Today, closed: 10-31 close against 10-30 close.
    today = items["XLK"]["periods"]["today"]
    assert (today["mode"], today["start_date"], today["end_date"]) == ("close", "2025-10-30", "2025-10-31")
    # Neither sample reaches back to the history start, so every ETF still needs its fetch;
    # the sectors without prices sort after the known values.
    assert set(result["prices_pending"]) == set(sector_etfs())
    assert [item["etf"] for item in result["items"][:2]] in (["XLK", "XLV"], ["XLV", "XLK"])
    assert items["XLF"]["periods"]["1m"]["status"] == "no_prices"


# --- today -------------------------------------------------------------------

def test_today_intraday_uses_the_quote_against_the_previous_close():
    data = flat(date(2025, 10, 1), date(2025, 10, 30))
    quote = {"regular_value": "101", "regular_time": et("2025-10-31T11:00:00").isoformat(),
             "fetched_at": et("2025-10-31T11:00:30").isoformat(), "status": "LIVE",
             "previous_close": "99"}
    result = today_change(data, min(data), quote, et("2025-10-31T11:01:00"))
    # The cache's 10-30 close (100) wins over Yahoo's previous close (99).
    assert result["mode"] == "intraday" and result["value"] == "0.010000"
    assert (result["start_date"], result["end_date"]) == ("2025-10-30", "2025-10-31")
    assert result["as_of"] == quote["regular_time"]
    without_cache = today_change({}, None, quote, et("2025-10-31T11:01:00"))
    assert without_cache["value"] == format(Decimal(101) / Decimal(99) - 1, ".6f")


def test_today_when_closed_compares_the_last_two_closes():
    data = flat(date(2025, 10, 1), date(2025, 10, 31))
    data[date(2025, 10, 31)] = Decimal(102)
    # Pre-market Monday: regular-session prices only.
    result = today_change(data, min(data), None, et("2025-11-03T08:00:00"))
    assert result == {"start_date": "2025-10-30", "end_date": "2025-10-31", "mode": "close",
                      "as_of": None, "delayed": False, "value": "0.020000", "status": "available"}


def test_today_after_the_close_uses_that_days_quote_until_the_cache_has_the_close():
    data = flat(date(2025, 10, 1), date(2025, 10, 30))
    quote = {"regular_value": "103", "regular_time": et("2025-10-31T16:00:00").isoformat(),
             "fetched_at": et("2025-10-31T16:30:00").isoformat(), "previous_close": "99",
             "status": "CLOSED"}
    result = today_change(data, min(data), quote, et("2025-10-31T17:00:00"))
    assert result["value"] == "0.030000" and result["mode"] == "close"
    # The next morning the same quote is no longer paired with a known previous close.
    stale = today_change({}, None, {**quote, "fetched_at": et("2025-11-03T04:05:00").isoformat()},
                         et("2025-11-03T08:00:00"))
    assert stale["value"] is None and stale["status"] == "missing"


# --- heatmap -----------------------------------------------------------------

def test_heatmap_weight_formula():
    # r_max − r_min = 1.5% exceeds the 1% floor for today.
    weights = heatmap_weights({"a": "0.010000", "b": "-0.005000", "c": "0.002000", "d": None},
                              "today")
    assert weights["b"] == 0.2 and weights["a"] == 1.0
    assert weights["c"] == pytest.approx(0.2 + 0.8 * 0.007 / 0.015)
    assert weights["d"] is None
    # A 0.1% spread on a 3-month period is measured against the 8% floor.
    small = heatmap_weights({"a": "0.001000", "b": "0.002000"}, "3m")
    assert small == {"a": 0.2, "b": pytest.approx(0.2 + 0.8 * 0.001 / 0.08)}
    assert heatmap_weights({"a": None}, "1w") == {"a": None}


# --- candles -----------------------------------------------------------------

def bars_for(start, end):
    return [{"date": day.isoformat(), "open": str(100 + index), "high": str(110 + index),
             "low": str(90 + index), "close": str(101 + index)}
            for index, day in enumerate(sessions(start, end))]


def test_week_and_month_candles_aggregate_daily_bars():
    # 2025-06-30 … 2025-07-11; July 4 is a holiday.
    bars = bars_for(date(2025, 6, 30), date(2025, 7, 11))
    weeks = aggregate(bars, "week")
    assert [(week["date"], week["end_date"], week["sessions"]) for week in weeks] == [
        ("2025-06-30", "2025-07-03", 4), ("2025-07-07", "2025-07-11", 5)]
    assert weeks[0] == {"date": "2025-06-30", "end_date": "2025-07-03", "open": "100",
                        "high": "113", "low": "90", "close": "104", "sessions": 4}
    months = aggregate(bars, "month")
    assert [(month["date"], month["open"], month["close"]) for month in months] == [
        ("2025-06-30", "100", "101"), ("2025-07-01", "101", "109")]
    assert aggregate(bars, "day")[0]["sessions"] == 1 and len(aggregate(bars, "day")) == 9


def test_candles_share_one_range_that_starts_with_whole_weeks():
    with session() as s, s.begin():
        save_cache(s, "XLK", sample("XLK"))
    with session() as s:
        body = candles(s, chart_range="3m", interval="week",
                       at=et("2025-11-01T12:00:00"))
    # 2025-10-31 − 3 months + 1 day = 2025-08-01 (Friday) → the week from Monday 07-28.
    assert (body["start_date"], body["end_date"]) == ("2025-07-28", "2025-10-31")
    assert len(body["items"]) == 11
    xlk = next(item for item in body["items"] if item["etf"] == "XLK")
    # The sample starts on Tuesday 2025-09-02 (Labor Day week).
    assert xlk["candles"][0]["date"] == "2025-09-02" and xlk["candles"][-1]["end_date"] == "2025-10-31"
    assert next(item for item in body["items"] if item["etf"] == "XLV")["candles"] == []


# --- no provider calls from reads; one fetch plan per day ---------------------

def app():
    from iirp.api.app import app as application
    return application


def test_switching_views_periods_and_ranges_never_requests_a_provider(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("a sector read must not call a provider")

    monkeypatch.setattr(yahoo, "fetch_market", forbidden)
    monkeypatch.setattr(stock_quotes, "fetch_stock_quotes", forbidden)
    with session() as s, s.begin():
        save_cache(s, "XLK", sample("XLK"))
    with session() as s:
        before = s.scalar(select(func.count()).select_from(Job))
    with TestClient(app()) as client:
        assert client.get("/api/v1/sectors").status_code == 200
        for chart_range in ("3m", "6m", "1y", "ytd"):
            for interval in ("day", "week", "month"):
                response = client.get("/api/v1/sectors/candles",
                                      params={"range": chart_range, "interval": interval})
                assert response.status_code == 200
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == before
        assert s.scalar(select(func.count()).select_from(Batch)) == 0


def test_price_request_plans_one_range_per_day():
    at = et("2026-10-09T10:00:00")
    with session() as s, s.begin():
        first = request_prices(s, foreground=True, at=at)
        again = request_prices(s, foreground=True, at=at)
        batch = s.get(Batch, first["batch_ids"][0])
        assert first == again and first["requested"] == sector_etfs()
        # This year and eight complete past years; the cache adds its month of buffer.
        assert batch.params["start_date"] == "2018-01-01"
        assert batch.params["end_date"] == "2026-10-09"
        assert batch.params["purpose"] == "sectors"
        assert s.scalar(select(func.count()).select_from(Batch)) == 1


def test_price_request_skips_covering_caches():
    at = datetime.now(timezone.utc)
    today = at.astimezone(ET).date()
    from iirp.analysis.calendar import last_completed_session

    completed = last_completed_session(at)
    bars = [{"date": day.isoformat(), "open": "1", "high": "1", "low": "1", "close": "1"}
            for day in sessions(date(today.year - 8, 1, 1) - timedelta(days=31), completed)]
    with session() as s, s.begin():
        for etf in sector_etfs():
            save_cache(s, etf, bars[-3:], start=date(today.year - 8, 1, 1) - timedelta(days=31))
    with session() as s, s.begin():
        assert request_prices(s, at=at) == {"requested": [], "batch_ids": []}


# --- home strip refresh cadence -----------------------------------------------

@pytest.mark.parametrize("at,again_after,period", [
    ("2026-10-09T11:00:00", 60, "regular"),
    ("2026-10-09T08:00:00", 300, "pre"),
    ("2026-10-09T17:00:00", 300, "post"),
])
def test_sector_quotes_refresh_every_minute_in_session_and_five_minutes_outside(
        monkeypatch, at, again_after, period):
    current = et(at)
    monkeypatch.setattr(stock_quotes, "now", lambda: current)
    assert stock_quotes.stock_session(current)[0] == period
    with session() as s, s.begin():
        assert len(request_stock_quotes(s, sector_etfs())) == 1
        job = s.scalar(select(Job).where(Job.kind == KIND))
        assert job.target["symbols"] == sorted(sector_etfs())
        job.status = "SUCCEEDED"
        due = stock_quotes.stock_session(current)[1]
        for etf in sector_etfs():
            s.add(MarketQuote(symbol=etf, fetched_at=current, data={
                "value": "1", "regular_value": "1", "regular_time": current.isoformat(),
                "next_refresh_at": due.isoformat()}))
    assert (due - current).total_seconds() == again_after
    for offset, jobs in ((again_after - 1, 0), (again_after, 1)):
        current = et(at) + timedelta(seconds=offset)
        with session() as s, s.begin():
            assert len(request_stock_quotes(s, sector_etfs())) == jobs


def test_sector_quotes_do_not_refresh_while_the_market_is_closed(monkeypatch):
    # Saturday: the saved quotes hold Friday's regular close.
    current = et("2026-10-10T12:00:00")
    monkeypatch.setattr(stock_quotes, "now", lambda: current)
    closing = et("2026-10-09T15:59:58")
    with session() as s, s.begin():
        for etf in sector_etfs():
            s.add(MarketQuote(symbol=etf, fetched_at=closing, data={
                "value": "1", "regular_value": "1", "regular_time": closing.isoformat(),
                "next_refresh_at": (closing + timedelta(minutes=5)).isoformat()}))
    with session() as s, s.begin():
        assert request_stock_quotes(s, sector_etfs()) == []


def test_saved_quotes_keep_yahoos_previous_close():
    fetched = et("2025-10-31T11:00:00")
    raw = {"regularMarketPrice": 101, "regularMarketTime": fetched.timestamp(),
           "regularMarketPreviousClose": 99.5}
    assert stock_quotes.stock_quote("XLK", raw, fetched)["previous_close"] == "99.5"
    raw["regularMarketPreviousClose"] = 0
    assert stock_quotes.stock_quote("XLK", raw, fetched)["previous_close"] is None
