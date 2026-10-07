"""Synthetic quote payloads never enter a database or contact Yahoo."""

from datetime import datetime, timezone

import pytest
from iirp.market.yahoo import quote_from_history

from tests.zh import zh


def snapshot(*, symbol="^GSPC", fetched="2026-09-16T15:00:00+00:00",
             quoted="2026-09-16T14:59:00+00:00", price=110, periods=True):
    meta = {"regularMarketPrice": price, "regularMarketTime": quoted,
            "exchangeTimezoneName": "America/New_York", "chartPreviousClose": 70}
    if periods:
        meta["currentTradingPeriod"] = {"regular": {
            "start": "2026-09-16T13:30:00+00:00", "end": "2026-09-16T20:00:00+00:00",
            "timezone": "EDT"}}
    return {"symbol": symbol, "fetched_at": fetched, "metadata": meta,
            "records": [{"date": "2026-09-14", "close": "95"},
                        {"date": "2026-09-15", "close": "100"},
                        {"date": "2026-09-16", "close": "109"}]}


def test_intraday_price_and_time_are_one_provider_pair():
    quote = quote_from_history("^GSPC", snapshot())
    assert quote["value"] == 110
    assert quote["source_time"] == "2026-09-16T14:59:00+00:00"
    assert quote["previous_close"] == "100"  # Not the month's chartPreviousClose=70.
    assert quote["change"] == 10 and quote["change_percent"] == 10
    assert quote["baseline_date"] == "2026-09-15"
    assert quote["market_open"] is True and quote["status"] == "DELAYED"
    assert quote["next_refresh_at"] == "2026-09-16T15:01:00+00:00"
    assert quote["records"][-1]["close"] == "109"  # Chart remains the actual daily series.


def test_old_daily_price_cannot_borrow_a_new_metadata_timestamp():
    payload = snapshot(price=None)
    payload["records"] = payload["records"][:2]
    quote = quote_from_history("^GSPC", payload)
    assert quote["value"] == 100 and quote["as_of"] == "2026-09-15"
    assert quote["source_time"] is None and quote["status"] == "DAILY"


@pytest.mark.parametrize("bad_time", [None, True, "not-a-time", "2026-09-16T14:59:00", 1e30,
                                      "2026-09-16T17:00:00+00:00"])
def test_unusable_or_future_quote_time_falls_back_without_claiming_freshness(bad_time):
    quote = quote_from_history("^GSPC", snapshot(quoted=bad_time))
    assert quote["value"] == 109
    assert quote["source_time"] is None and quote["status"] == "DAILY"


def test_quote_without_daily_baseline_keeps_value_but_not_invented_change():
    payload = snapshot()
    payload["records"] = []
    quote = quote_from_history("^GSPC", payload)
    assert quote["value"] == 110
    assert quote["previous_close"] is None
    assert quote["change"] is None and quote["change_percent"] is None


def test_closed_quote_keeps_source_time_and_checks_every_five_minutes_after_hours():
    quote = quote_from_history("^GSPC", snapshot(fetched="2026-09-16T21:00:00+00:00",
                                                quoted="2026-09-16T20:00:00+00:00"))
    assert quote["status"] == "CLOSED" and quote["market_open"] is False
    assert quote["source_time"] == "2026-09-16T20:00:00+00:00"
    assert quote["previous_close"] == "100"
    assert quote["next_refresh_at"] == "2026-09-16T21:05:00+00:00"


@pytest.mark.parametrize(("fetched", "due"), [
    ("2026-09-17T01:00:00+00:00", "2026-09-17T08:00:00+00:00"),  # 21:00 ET: next pre-market 04:00
    ("2026-09-19T15:00:00+00:00", "2026-09-21T08:00:00+00:00"),  # Saturday: Monday 04:00 ET
])
def test_closed_market_waits_for_the_next_extended_window(fetched, due):
    quote = quote_from_history("^GSPC", snapshot(fetched=fetched, quoted="2026-09-16T20:00:00+00:00"))
    assert quote["status"] == "CLOSED"
    assert quote["next_refresh_at"] == due


def test_source_snapshot_stale_during_active_period_is_labelled():
    quote = quote_from_history("^GSPC", snapshot(quoted="2026-09-15T20:00:00+00:00"))
    assert quote["status"] == "STALE" and quote["market_open"] is True
    assert quote["as_of"] == "2026-09-15" and quote["previous_close"] == "95"


def test_unknown_session_does_not_claim_closed_or_equity_hours():
    quote = quote_from_history("^VIX", snapshot(periods=False))
    assert quote["market_open"] is None and quote["status"] == "DELAYED"
    assert quote["session"] is None
    assert quote["next_refresh_at"] == "2026-09-16T15:05:00+00:00"


def test_futures_cross_midnight_uses_own_provider_session_and_trading_day():
    payload = snapshot(symbol="GC=F", fetched="2026-09-16T23:00:00+00:00",
                       quoted="2026-09-16T22:59:00+00:00")
    payload["metadata"]["currentTradingPeriod"]["regular"] = {
        "start": "2026-09-16T22:00:00+00:00", "end": "2026-09-17T21:00:00+00:00"}
    payload["records"].append({"date": "2026-09-17", "close": "110"})
    quote = quote_from_history("GC=F", payload)
    assert quote["market_open"] is True and quote["status"] == "DELAYED"
    assert quote["as_of"] == "2026-09-17"
    assert quote["previous_close"] == "109" and quote["baseline_date"] == "2026-09-16"
    assert "期货" in zh(quote["instrument"]) and zh(quote["unit"]) == "美元/盎司"


def test_vix_provider_session_can_be_active_after_stock_close():
    payload = snapshot(symbol="^VIX", fetched="2026-09-16T20:10:00+00:00",
                       quoted="2026-09-16T20:09:00+00:00")
    payload["metadata"]["currentTradingPeriod"]["regular"]["end"] = "2026-09-16T20:15:00+00:00"
    assert quote_from_history("^VIX", payload)["market_open"] is True
    assert quote_from_history("^GSPC", snapshot(fetched=payload["fetched_at"],
                                              quoted=payload["metadata"]["regularMarketTime"]))["market_open"] is False


def test_next_provider_session_start_bounds_the_premarket_step():
    payload = snapshot(fetched="2026-09-16T13:00:00+00:00", quoted="2026-09-15T20:00:00+00:00")
    quote = quote_from_history("^GSPC", payload)
    assert quote["next_refresh_at"] == "2026-09-16T13:05:00+00:00"
    assert quote["market_open"] is False
    payload = snapshot(fetched="2026-09-16T13:28:00+00:00", quoted="2026-09-15T20:00:00+00:00")
    assert quote_from_history("^GSPC", payload)["next_refresh_at"] == "2026-09-16T13:30:00+00:00"


def test_empty_unusable_source_cannot_replace_previous_good_snapshot():
    payload = snapshot(price=None)
    payload["records"] = []
    assert quote_from_history("^GSPC", payload) is None


def test_quote_fetch_is_current_and_bounded_even_when_target_is_old(monkeypatch):
    import pandas as pd
    import yfinance
    from iirp.market import yahoo

    captured = []
    frame = pd.DataFrame({"Close": [100.0]}, index=pd.to_datetime(["2026-09-16"]))

    class Ticker:
        history_metadata = snapshot()["metadata"]

        def __init__(self, symbol, session):
            pass

        def history(self, **parameters):
            captured.append(parameters)
            return frame

    monkeypatch.setattr(yfinance, "Ticker", Ticker)
    monkeypatch.setattr(yfinance, "set_tz_cache_location", lambda _: None)
    monkeypatch.setattr(yahoo, "now", lambda: datetime(2026, 9, 16, 15, tzinfo=timezone.utc))
    result = yahoo.fetch_market("market_quote", {"symbol": "^GSPC", "start_date": "2018-01-01",
                                                     "end_date": "2026-09-15"})
    assert len(captured) == 1
    assert captured[0]["start"] == "2026-08-07"
    assert captured[0]["end"] == "2026-09-18"
    assert captured[0]["interval"] == "1d" and captured[0]["auto_adjust"] is False
    assert result["metadata"]["regularMarketPrice"] == 110


def test_research_history_fetch_keeps_frozen_dates(monkeypatch):
    import pandas as pd
    import yfinance
    from iirp.market import yahoo

    captured = []

    class Ticker:
        history_metadata = snapshot()["metadata"]

        def __init__(self, symbol, session):
            pass

        def history(self, **parameters):
            captured.append(parameters)
            return pd.DataFrame({"Close": [100.0]}, index=pd.to_datetime(["2026-09-15"]))

    monkeypatch.setattr(yfinance, "Ticker", Ticker)
    monkeypatch.setattr(yfinance, "set_tz_cache_location", lambda _: None)
    yahoo.fetch_market("market_history", {"symbol": "AAPL", "start_date": "2018-01-01",
                                                "end_date": "2026-09-15"})
    assert captured[0]["start"] == "2018-01-01"
    assert captured[0]["end"] == "2026-09-16"


def test_invalid_provider_session_remains_unknown():
    payload = snapshot()
    payload["metadata"]["currentTradingPeriod"]["regular"]["end"] = "2026-09-15T20:00:00+00:00"
    quote = quote_from_history("^GSPC", payload)
    assert quote["market_open"] is None and quote["session"] is None


def test_numeric_and_offset_timestamps_identify_the_same_instant():
    payload = snapshot(quoted=int(datetime(2026, 9, 16, 14, 59, tzinfo=timezone.utc).timestamp()))
    payload["metadata"]["currentTradingPeriod"]["regular"]["start"] = "2026-09-16T09:30:00-04:00"
    quote = quote_from_history("^GSPC", payload)
    assert quote["source_time"] == "2026-09-16T14:59:00+00:00"
    assert quote["session"]["start"] == "2026-09-16T13:30:00+00:00"


def test_regular_quote_does_not_treat_generic_index_premarket_as_active():
    payload = snapshot(fetched="2026-09-16T12:00:00+00:00", quoted="2026-09-15T20:00:00+00:00")
    payload["metadata"]["currentTradingPeriod"]["pre"] = {
        "start": "2026-09-16T08:00:00+00:00", "end": "2026-09-16T13:30:00+00:00"}
    quote = quote_from_history("^GSPC", payload)
    assert quote["market_open"] is False and quote["status"] == "CLOSED"
    assert quote["next_refresh_at"] == "2026-09-16T12:05:00+00:00"


@pytest.mark.parametrize("zone", [None, "not/a-timezone"])
def test_missing_exchange_zone_keeps_source_instant_without_guessing_baseline(zone):
    payload = snapshot()
    payload["metadata"]["exchangeTimezoneName"] = zone
    quote = quote_from_history("^GSPC", payload)
    assert quote["value"] == 110 and quote["source_time"] == "2026-09-16T14:59:00+00:00"
    assert quote["change_percent"] is None and quote["baseline_date"] is None


def test_missing_real_exchange_session_does_not_use_older_close_as_daily_change():
    payload = snapshot(fetched="2026-09-23T22:00:00+00:00", quoted="2026-09-23T20:00:00+00:00", price=110)
    payload["records"] = [{"date": "2026-09-21", "close": "100"},
                          {"date": "2026-09-22", "close": None, "volume": 0},
                          {"date": "2026-09-23", "close": "109"}]
    quote = quote_from_history("^GSPC", payload)
    assert quote["baseline_gap_date"] == "2026-09-22"
    assert quote["last_available_close_date"] == "2026-09-21"
    assert quote["previous_close"] is quote["change_percent"] is None
    assert "无法计算单日变化" in zh(quote["baseline"])


def test_weekend_is_not_a_missing_exchange_session():
    payload = snapshot(fetched="2026-09-21T22:00:00+00:00", quoted="2026-09-21T20:00:00+00:00", price=110)
    payload["records"] = [{"date": "2026-09-18", "close": "100"}, {"date": "2026-09-21", "close": "109"}]
    quote = quote_from_history("^GSPC", payload)
    assert quote["baseline_gap_date"] is None
    assert quote["baseline_date"] == "2026-09-18" and quote["change_percent"] == 10
