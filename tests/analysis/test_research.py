"""Calendar and Insider transaction-window examples (synthetic, hand-computed).

Monthly and interval research examples are in test_research_open_close.py.
"""

import json
from datetime import date, datetime

import pytest
from iirp.analysis.calendar import (
    last_completed_session,
    next_regular_open_after,
    reaction_session,
    session_bounds,
    sessions,
)
from iirp.analysis.transaction_windows import transaction_price_context


def bar(day, close="100", opening=None, status="VALID"):
    return {
        "date": str(day),
        "open": opening or close,
        "high": close,
        "low": close,
        "close": close,
        "status": status,
    }


def history(start, end, overrides=None):
    overrides = overrides or {}
    return [bar(day, overrides.get(day.isoformat(), "100")) for day in sessions(start, end)]


def test_exchange_calendar_good_friday_and_early_close():
    assert sessions(date(2024, 3, 28), date(2024, 4, 1)) == [date(2024, 3, 28), date(2024, 4, 1)]
    opening, closing = session_bounds(date(2024, 11, 29))
    assert (opening.hour, opening.minute, closing.hour) == (9, 30, 13)
    assert closing.utcoffset().total_seconds() == -5 * 3600
    assert session_bounds(date(2024, 7, 3))[1].hour == 13
    assert session_bounds(date(2024, 7, 3))[1].utcoffset().total_seconds() == -4 * 3600


@pytest.mark.parametrize(
    ("instant", "reaction", "precise", "status"),
    [
        ("2024-11-29T08:00:00-05:00", "2024-11-29", True, "available"),
        ("2024-11-29T09:30:00-05:00", "2024-11-29", False, "intraday_observation"),
        ("2024-11-29T12:59:59-05:00", "2024-11-29", False, "intraday_observation"),
        ("2024-11-29T13:00:00-05:00", "2024-12-02", True, "available"),
        ("2024-11-28T09:00:00-05:00", "2024-11-29", True, "available"),
        ("2024-03-29T16:00:00-04:00", "2024-04-01", True, "available"),
        ("2024-03-11T13:00:00Z", "2024-03-11", True, "available"),
    ],
)
def test_reaction_respects_exact_open_close_holidays_and_dst(instant, reaction, precise, status):
    result = reaction_session(instant)
    assert result["reaction_date"] == reaction
    assert result["opening_attribution"] is precise
    assert result["status"] == status


def test_completed_day_uses_actual_close_and_next_open_is_after_timestamp():
    assert last_completed_session(datetime.fromisoformat("2024-11-29T12:59:00-05:00")) == date(
        2024, 11, 27
    )
    assert last_completed_session(datetime.fromisoformat("2024-11-29T13:00:00-05:00")) == date(
        2024, 11, 29
    )
    assert next_regular_open_after("2024-11-29T09:29:59-05:00") == date(2024, 11, 29)
    assert next_regular_open_after("2024-11-29T09:30:00-05:00") == date(2024, 12, 2)


def test_date_only_timestamp_and_conflicting_event_fields_do_not_invent_precision():
    result = reaction_session("2024-01-03")
    assert result["opening_attribution"] is False
    mismatch = reaction_session("2024-01-03T16:30:00-05:00", time_precision="before_open")
    assert mismatch["opening_attribution"] is False
    assert mismatch["status"] == "conflicting_event_time"
    date_mismatch = reaction_session("2024-01-03T08:00:00-05:00", announced_date="2024-01-02")
    assert date_mismatch["opening_attribution"] is False
    context = transaction_price_context(
        history(date(2023, 12, 1), date(2024, 1, 31)),
        "2024-01-02",
        "2024-01-03",
        today=date(2024, 1, 31),
    )
    assert context["accepted_at"] is None
    assert context["accepted_date"] == "2024-01-03"
    assert context["next_open"] is None
    assert context["disclosure"]["opening_gap"] is None


def test_transaction_dual_baselines_and_next_open_after_intraday_disclosure():
    prices = history(
        date(2023, 12, 1),
        date(2024, 1, 10),
        {
            "2024-01-02": "100",
            "2024-01-03": "110",
            "2024-01-04": "120",
            "2024-01-10": "132",
        },
    )
    next(row for row in prices if row["date"] == "2024-01-04")["open"] = "110"
    result = transaction_price_context(
        prices, "2024-01-02", "2024-01-03T12:00:00-05:00", today=date(2024, 1, 10)
    )
    assert result["transaction"]["baseline_date"] == "2024-01-02"
    assert next(point for point in result["transaction"]["points"] if point["x"] == 0)["close"] == result["transaction"]["baseline_close"]
    assert result["transaction"]["day_1"] == "0.1"
    assert result["disclosure"]["baseline_date"] == "2024-01-02"
    assert result["disclosure"]["day_1_date"] == "2024-01-03"
    assert result["disclosure"]["opening_gap"] is None
    assert result["next_open"]["start_date"] == "2024-01-04"
    assert result["next_open"]["day_5_date"] == "2024-01-10"
    assert result["next_open"]["day_5"] == "0.2"
    json.dumps(result, allow_nan=False)


def test_non_session_transaction_and_future_window_never_use_supplied_future_bars():
    prices = history(date(2023, 12, 1), date(2024, 1, 31))
    result = transaction_price_context(
        prices, "2024-01-06", "2024-01-08T17:00:00-05:00", today=date(2024, 1, 8)
    )
    assert result["transaction"]["non_session_transaction"] is True
    assert result["transaction"]["observation_anchor"] == "2024-01-08"
    assert result["transaction"]["day_1"] is None
    assert result["transaction"]["day_1_status"] == "not_yet_formed"
    assert result["disclosure"]["mature_sessions"] == 0
    assert result["next_open"]["opening_price"] is None
