"""Home market strip: instrument catalog, own-session rules for futures, ^TNX units and the
display setting that chooses the instruments.

Pure tests use synthetic Yahoo payloads; the API tests use a disposable iirp_v1_test_*
database. Nothing contacts Yahoo.
"""

import json
from datetime import datetime, timezone

import pytest
from iirp.config import ROOT
from iirp.db import session
from iirp.jobs import auto_update
from iirp.market.instruments import (
    DEFAULT_SYMBOLS,
    INSTRUMENTS,
    fetch_symbols,
    shown_symbols,
    us_regular_session,
)
from iirp.market.reads import _display_fields, _quote_freshness
from iirp.market.yahoo import MARKETS, quote_from_history
from iirp.messages import decode
from iirp.models import MarketQuote

from tests.jobs.test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_client,
    lifecycle_database,
)

DEFAULT_STRIP = ["^GSPC", "^IXIC", "^NDX", "^DJI", "^RUT", "^VIX", "^TNX", "DX-Y.NYB", "GC=F", "CL=F"]


def utc(text):
    return datetime.fromisoformat(text).astimezone(timezone.utc)


# CME equity futures: Sunday-Thursday 18:00 ET to 17:00 ET the next day.
ES_SESSION = {"start": "2026-10-07T22:00:00+00:00", "end": "2026-10-08T21:00:00+00:00"}
FRIDAY_SESSION = {"start": "2026-10-08T22:00:00+00:00", "end": "2026-10-09T21:00:00+00:00"}


def test_catalog_defaults_and_bitcoin_off():
    assert [item.symbol for item in INSTRUMENTS if item.default and not item.off_hours_only] == DEFAULT_STRIP
    assert DEFAULT_SYMBOLS == [*DEFAULT_STRIP, "ES=F", "NQ=F"]
    assert "BTC-USD" not in DEFAULT_SYMBOLS and "BTC-USD" in MARKETS
    assert set(MARKETS) == {item.symbol for item in INSTRUMENTS}


def test_catalog_messages_are_translated():
    codes = {code for item in INSTRUMENTS for code in (item.name, item.unit, item.instrument, item.short) if code}
    for language in ("en", "zh"):
        keys = json.loads((ROOT / f"frontend/src/locales/{language}/translation.json").read_text())
        assert not sorted(codes - set(keys)), language


@pytest.mark.parametrize(("stamp", "regular"), [
    ("2026-10-07T11:00:00-04:00", True),    # Wednesday in session
    ("2026-10-07T09:29:00-04:00", False),   # pre-market
    ("2026-10-07T16:00:00-04:00", False),   # the close
    ("2026-10-10T11:00:00-04:00", False),   # Saturday
    ("2026-11-26T11:00:00-05:00", False),   # Thanksgiving
    ("2026-11-27T12:30:00-05:00", True),    # early close at 13:00
    ("2026-11-27T13:30:00-05:00", False),
])
def test_us_regular_session_follows_the_nyse_calendar(stamp, regular):
    assert us_regular_session(utc(stamp)) is regular


def test_index_futures_hide_and_wait_while_us_stocks_trade():
    quotes = {"ES=F": {"session": ES_SESSION}, "NQ=F": {"session": ES_SESSION}}
    during = utc("2026-10-08T11:00:00-04:00")
    assert fetch_symbols(DEFAULT_SYMBOLS, during) == DEFAULT_STRIP
    assert shown_symbols(DEFAULT_SYMBOLS, quotes, during) == DEFAULT_STRIP


def test_index_futures_show_outside_us_hours_only_while_they_trade():
    quotes = {"ES=F": {"session": ES_SESSION}, "NQ=F": {"session": ES_SESSION}}
    evening = utc("2026-10-07T20:30:00-04:00")
    assert shown_symbols(DEFAULT_SYMBOLS, quotes, evening) == [*DEFAULT_STRIP, "ES=F", "NQ=F"]
    assert fetch_symbols(DEFAULT_SYMBOLS, evening) == [*DEFAULT_STRIP, "ES=F", "NQ=F"]
    # The daily pause 17:00-18:00 ET: the saved session has ended, so they are hidden,
    # but still fetched when due so the strip learns when they reopen.
    pause = utc("2026-10-08T17:30:00-04:00")
    assert shown_symbols(DEFAULT_SYMBOLS, quotes, pause) == DEFAULT_STRIP
    assert "ES=F" in fetch_symbols(DEFAULT_SYMBOLS, pause)
    # A futures contract with no saved quote yet is not shown.
    assert shown_symbols(DEFAULT_SYMBOLS, {}, evening) == DEFAULT_STRIP


def test_weekend_hides_futures_and_shows_closed_indexes():
    saturday = utc("2026-10-10T12:00:00-04:00")
    quotes = {"ES=F": {"session": FRIDAY_SESSION}, "NQ=F": {"session": FRIDAY_SESSION}}
    assert shown_symbols(DEFAULT_SYMBOLS, quotes, saturday) == DEFAULT_STRIP
    index = {"fetched_at": "2026-10-09T21:00:00+00:00", "status": "CLOSED",
             "session": {"start": "2026-10-09T13:30:00+00:00", "end": "2026-10-09T20:00:00+00:00"}}
    assert _quote_freshness(index, None, saturday) == ("closed", None)


def snapshot(symbol, *, fetched, quoted, price, period, records):
    return {"symbol": symbol, "fetched_at": fetched, "records": records,
            "metadata": {"regularMarketPrice": price, "regularMarketTime": quoted,
                         "exchangeTimezoneName": "America/New_York",
                         "currentTradingPeriod": {"regular": {**period, "timezone": "EDT"}}}}


FUTURES_RECORDS = [{"date": "2026-10-08", "close": "6700"}, {"date": "2026-10-09", "close": "6710"}]


def test_closed_futures_are_due_at_their_sunday_reopen_not_at_monday_stock_hours():
    payload = snapshot("ES=F", fetched="2026-10-10T16:00:00+00:00", quoted="2026-10-09T21:00:00+00:00",
                       price=6710, period=FRIDAY_SESSION, records=FUTURES_RECORDS)
    quote = quote_from_history("ES=F", payload)
    assert quote["market_open"] is False
    assert quote["next_refresh_at"] == "2026-10-11T22:00:00+00:00"  # Sunday 18:00 ET
    assert decode(quote["baseline"])["code"] == "quote.baseline.previous_futures"
    # A stock index on the same Saturday waits for Monday's pre-market.
    index = quote_from_history("^GSPC", snapshot(
        "^GSPC", fetched="2026-10-10T16:00:00+00:00", quoted="2026-10-09T20:00:00+00:00", price=6700,
        period={"start": "2026-10-09T13:30:00+00:00", "end": "2026-10-09T20:00:00+00:00"},
        records=[{"date": "2026-10-08", "close": "6650"}, {"date": "2026-10-09", "close": "6700"}]))
    assert index["status"] == "CLOSED" and index["next_refresh_at"] == "2026-10-12T08:00:00+00:00"


def test_bitcoin_follows_its_own_round_the_clock_session():
    period = {"start": "2026-10-10T00:00:00+00:00", "end": "2026-10-10T23:59:00+00:00"}
    quote = quote_from_history("BTC-USD", snapshot(
        "BTC-USD", fetched="2026-10-10T16:00:00+00:00", quoted="2026-10-10T15:59:30+00:00", price=62000,
        period=period, records=[{"date": "2026-10-09", "close": "61000"}, {"date": "2026-10-10", "close": "62000"}]))
    assert quote["market_open"] is True
    assert quote["next_refresh_at"] == "2026-10-10T16:01:00+00:00"
    assert decode(quote["unit"])["code"] == "quote.unit.usd"


def test_ten_year_yield_is_a_percent_with_basis_point_changes():
    quote = quote_from_history("^TNX", snapshot(
        "^TNX", fetched="2026-10-07T15:00:00+00:00", quoted="2026-10-07T14:59:00+00:00", price=4.123,
        period={"start": "2026-10-07T12:00:00+00:00", "end": "2026-10-07T21:00:00+00:00"},
        records=[{"date": "2026-10-06", "close": "4.092"}, {"date": "2026-10-07", "close": "4.12"}]))
    assert decode(quote["unit"])["code"] == "quote.unit.percent_yield"
    assert quote["value"] == pytest.approx(4.123) and quote["change"] == pytest.approx(0.031)
    assert _display_fields("^TNX", quote) == {"kind": "yield", "unit": quote["unit"], "change_bp": 3.1,
                                              "short_name": quote["name"]}
    assert "change_bp" not in _display_fields("^GSPC", {"change": 12.5})
    assert _display_fields("CL=F", {})["kind"] == "futures"
    assert decode(_display_fields("CL=F", {})["unit"])["code"] == "quote.unit.usd_per_barrel"


# ---- Display setting and the home endpoint (database). ----


def save_quote(symbol, data):
    with session() as s, s.begin():
        s.merge(MarketQuote(symbol=symbol, data=data, fetched_at=datetime.fromisoformat(data["fetched_at"])))


def test_strip_choice_round_trips_and_drives_the_home_strip(lifecycle_client, monkeypatch):  # noqa: F811
    state = lifecycle_client.get("/api/v1/display").json()
    assert state["market_strip"]["symbols"] == DEFAULT_SYMBOLS
    catalog = {item["symbol"]: item for item in state["instruments"]}
    assert catalog["BTC-USD"]["default"] is False and catalog["ES=F"]["off_hours_only"] is True

    # During US hours: the ten defaults, in order; nothing about futures.
    monkeypatch.setattr("iirp.api.reads.now", lambda: utc("2026-10-07T11:00:00-04:00"))
    home = lifecycle_client.get("/api/v1/home").json()
    assert [quote["symbol"] for quote in home["market"]] == DEFAULT_STRIP
    assert all(quote["freshness"] == "missing" for quote in home["market"])
    assert next(quote for quote in home["market"] if quote["symbol"] == "^TNX")["kind"] == "yield"

    saved = lifecycle_client.put("/api/v1/display", json={"market_strip": {"symbols": ["BTC-USD", "^GSPC", "^TNX"]}})
    assert saved.status_code == 200
    assert saved.json()["market_strip"]["symbols"] == ["^GSPC", "^TNX", "BTC-USD"]
    # Colors were not part of the request and keep their value.
    assert saved.json()["colors"]["preset"] == "us"
    home = lifecycle_client.get("/api/v1/home").json()
    assert [quote["symbol"] for quote in home["market"]] == ["^GSPC", "^TNX", "BTC-USD"]

    for body, code in (({"symbols": ["^GSPC", "AAPL"]}, "display.instrument_unknown"),
                       ({"symbols": []}, "display.strip_empty")):
        refused = lifecycle_client.put("/api/v1/display", json={"market_strip": body})
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == code


def test_home_shows_index_futures_in_the_evening_while_they_trade(lifecycle_client, monkeypatch):  # noqa: F811
    for symbol in ("ES=F", "NQ=F"):
        save_quote(symbol, {"symbol": symbol, "value": 6720.5, "change": 10.5, "change_percent": 0.16,
                            "fetched_at": "2026-10-08T00:29:00+00:00", "status": "DELAYED",
                            "session": ES_SESSION, "next_refresh_at": "2026-10-08T00:30:00+00:00"})
    monkeypatch.setattr("iirp.api.reads.now", lambda: utc("2026-10-07T20:30:00-04:00"))
    shown = [quote["symbol"] for quote in lifecycle_client.get("/api/v1/home").json()["market"]]
    assert shown == [*DEFAULT_STRIP, "ES=F", "NQ=F"]
    monkeypatch.setattr("iirp.api.reads.now", lambda: utc("2026-10-08T11:00:00-04:00"))
    shown = [quote["symbol"] for quote in lifecycle_client.get("/api/v1/home").json()["market"]]
    assert shown == DEFAULT_STRIP


def test_only_chosen_and_due_instruments_are_requested(lifecycle_client, monkeypatch):  # noqa: F811
    clock = {"now": utc("2026-10-07T11:00:00-04:00")}
    monkeypatch.setattr(auto_update, "now", lambda: clock["now"])
    with session() as s:
        assert auto_update._market_due(s) == DEFAULT_STRIP
    # A fresh quote is not due again until its next refresh time.
    save_quote("^GSPC", {"symbol": "^GSPC", "value": 6700, "fetched_at": "2026-10-07T14:59:30+00:00",
                         "next_refresh_at": "2026-10-07T15:00:30+00:00"})
    with session() as s:
        assert "^GSPC" not in auto_update._market_due(s)
    lifecycle_client.put("/api/v1/display", json={"market_strip": {"symbols": ["^GSPC", "BTC-USD", "ES=F"]}})
    with session() as s:
        assert auto_update._market_due(s) == ["BTC-USD"]
    clock["now"] = utc("2026-10-07T20:30:00-04:00")
    with session() as s:
        assert auto_update._market_due(s) == ["^GSPC", "ES=F", "BTC-USD"]
