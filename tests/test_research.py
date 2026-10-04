"""Synthetic, hand-computed research examples; never live provider fixtures."""

import json
from datetime import date, datetime
from decimal import Decimal

import pytest
from iirp.analytics.calendar import (
    last_completed_session,
    next_regular_open_after,
    reaction_session,
    session_bounds,
    session_window,
    sessions,
)
from iirp.analytics.research import (
    compute_research,
    plan_scope,
    transaction_price_context,
)


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


def historical(result, year):
    return next(row for row in result["rows"] if row["year"] == year)


def point(result, year, x):
    series = next(item for item in result["series"] if item["year"] == year)
    return next(item for item in series["points"] if item["x"] == x)


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


def test_scope_expands_8_to_12_to_3_and_adds_previous_month_baseline():
    params = {"kind": "monthly", "current_year": 2026}
    assert plan_scope({**params, "historical_years": 8}, date(2026, 9, 1))[0] == date(2017, 12, 29)
    assert plan_scope({**params, "historical_years": 12}, date(2026, 9, 1))[0] == date(2013, 12, 31)
    assert plan_scope({**params, "historical_years": 3}, date(2026, 9, 1))[0] == date(2022, 12, 30)
    assert plan_scope({**params, "historical_years": 25}, date(2026, 9, 1))[0] == date(2000, 12, 29)


def test_public_optional_nulls_do_not_disable_defaults_or_automatic_cross_year():
    params = {
        "kind": "interval",
        "historical_years": 1,
        "start_mmdd": "12-15",
        "end_mmdd": "01-20",
        "cross_year": None,
        "current_year": None,
        "years": None,
    }
    start, end = plan_scope(params, date(2024, 1, 10))
    assert (start, end) == (date(2022, 12, 15), date(2024, 1, 10))
    result = compute_research(params, [], today=date(2024, 1, 10))
    assert result["metadata"]["current_year"] == 2023
    assert result["metadata"]["historical_years"] == [2022]


def monthly_example():
    return history(
        date(2021, 12, 31),
        date(2024, 1, 31),
        {
            "2022-01-31": "110",
            "2023-01-31": "90",
            "2024-01-31": "1100",
        },
    )


def test_monthly_baseline_historical_quantiles_and_current_separation():
    result = compute_research(
        {
            "kind": "monthly",
            "years": [2022, 2023],
            "current_year": 2024,
            "month": 1,
            "comparison": "complete",
        },
        monthly_example(),
        today=date(2024, 2, 1),
    )
    assert result["effective_n"] == 2
    assert historical(result, 2022)["baseline_date"] == "2021-12-31"
    assert Decimal(historical(result, 2022)["endpoint"]) == Decimal("0.1")
    assert Decimal(result["summary"]["median"]) == 0
    assert Decimal(result["summary"]["q25"]) == Decimal("-0.05")
    assert Decimal(result["summary"]["q75"]) == Decimal("0.05")
    assert result["summary"]["up"] == 1
    assert Decimal(result["summary"]["current"]["endpoint"]) == 10
    assert len(result["cells"]) == 36
    assert (
        next(cell for cell in result["cells"] if cell["year"] == 2024 and cell["month"] == 3)[
            "status"
        ]
        == "not_started"
    )
    json.dumps(result, allow_nan=False)


def test_missing_session_is_not_weekend_forward_fill_and_endpoint_is_partial_only():
    prices = [row for row in monthly_example() if row["date"] != "2022-01-14"]
    result = compute_research(
        {
            "kind": "monthly",
            "years": [2022, 2023],
            "current_year": 2024,
            "month": 1,
            "comparison": "complete",
        },
        prices,
        today=date(2024, 2, 1),
    )
    assert result["effective_n"] == 1
    assert historical(result, 2022)["endpoint"] == "0.1"
    assert historical(result, 2022)["max_drawdown"] is None
    assert point(result, 2022, 14)["status"] == "missing_price"
    assert point(result, 2022, 15)["status"] == "missing_price"
    assert point(result, 2022, 13)["value"] == "0"
    assert point(result, 2023, 15)["status"] == "non_session_carry"
    assert next(item for item in result["summary"]["path"] if item["x"] == 15)["n"] == 1


def test_monthly_first_day_close_does_not_replace_missing_prior_close():
    result = compute_research(
        {
            "kind": "monthly",
            "years": [2023],
            "current_year": 2024,
            "month": 1,
        },
        history(date(2023, 1, 1), date(2023, 1, 31)),
        today=date(2024, 2, 1),
    )
    row = historical(result, 2023)
    assert row["status"] == "missing_baseline"
    assert row["endpoint"] is None
    assert result["effective_n"] == 0


def test_same_progress_calendar_and_trading_alignment_use_different_correct_cutoffs():
    prices = history(
        date(2022, 12, 30), date(2024, 1, 5), {"2023-01-05": "110", "2023-01-06": "120"}
    )
    params = {
        "kind": "monthly",
        "years": [2023],
        "current_year": 2024,
        "month": 1,
        "comparison": "same_progress",
    }
    by_date = compute_research({**params, "alignment": "calendar"}, prices, today=date(2024, 1, 5))
    by_session = compute_research(
        {**params, "alignment": "trading"}, prices, today=date(2024, 1, 5)
    )
    # Jan 2–5 2024 contains four sessions; the fourth Jan 2023 session is Jan 6.
    assert historical(by_date, 2023)["actual_end"] == "2023-01-05"
    assert historical(by_date, 2023)["endpoint"] == "0.1"
    assert historical(by_session, 2023)["actual_end"] == "2023-01-06"
    assert historical(by_session, 2023)["endpoint"] == "0.2"


def test_interval_missing_first_expected_close_cannot_shift_and_no_sessions_is_not_zero():
    params = {
        "kind": "interval",
        "years": [2023],
        "current_year": 2024,
        "start_mmdd": "01-03",
        "end_mmdd": "01-06",
        "comparison": "complete",
    }
    result = compute_research(
        params,
        [bar("2023-01-04", "100"), bar("2023-01-05", "200"), bar("2023-01-06", "150")],
        today=date(2024, 2, 1),
    )
    row = historical(result, 2023)
    assert row["actual_start"] == "2023-01-03"
    assert row["baseline_date"] == "2023-01-03"
    assert row["endpoint"] is None
    assert row["status"] == "missing_baseline"
    empty = compute_research(
        {**params, "start_mmdd": "01-07", "end_mmdd": "01-08"}, [], today=date(2024, 2, 1)
    )
    assert historical(empty, 2023)["status"] == "no_sessions"
    assert historical(empty, 2023)["endpoint"] is None


def test_interval_hand_calculated_drawdown_and_single_session():
    params = {
        "kind": "interval",
        "years": [2023],
        "current_year": 2024,
        "start_mmdd": "01-03",
        "end_mmdd": "01-06",
        "comparison": "complete",
    }
    result = compute_research(
        params,
        [
            bar("2023-01-03", "100"),
            bar("2023-01-04", "120"),
            bar("2023-01-05", "90"),
            bar("2023-01-06", "110"),
        ],
        today=date(2024, 2, 1),
    )
    row = historical(result, 2023)
    assert (row["endpoint"], row["relative_high"], row["relative_low"], row["max_drawdown"]) == (
        "0.1",
        "0.2",
        "-0.1",
        "0.25",
    )
    single = compute_research(
        {**params, "end_mmdd": "01-03"}, [bar("2023-01-03", "100")], today=date(2024, 2, 1)
    )
    assert historical(single, 2023)["endpoint"] == "0"
    assert historical(single, 2023)["single_session"] is True


def test_interval_nonleap_feb29_and_leap_monthday_progress_do_not_peek():
    params = {
        "kind": "interval",
        "years": [2023],
        "current_year": 2024,
        "start_mmdd": "02-29",
        "end_mmdd": "03-02",
        "comparison": "same_progress",
    }
    result = compute_research(
        params, history(date(2023, 2, 28), date(2024, 3, 1)), today=date(2024, 3, 1)
    )
    row = historical(result, 2023)
    assert row["start_date"] == "2023-02-28"
    assert row["actual_end"] == "2023-03-01"
    assert row["expected_sessions"] == 2


def test_calendar_interval_paths_align_march_positions_across_leap_years():
    result = compute_research(
        {
            "kind": "interval",
            "years": [2023],
            "current_year": 2024,
            "start_mmdd": "02-28",
            "end_mmdd": "03-01",
            "comparison": "complete",
        },
        history(date(2023, 2, 28), date(2024, 3, 1)),
        today=date(2024, 3, 2),
    )
    assert point(result, 2023, 2)["date"] == "2023-03-01"
    assert point(result, 2024, 2)["date"] == "2024-03-01"


def test_cross_year_current_in_january_and_cutoff_in_historical_year():
    result = compute_research(
        {
            "kind": "interval",
            "historical_years": 1,
            "start_mmdd": "12-15",
            "end_mmdd": "01-20",
            "comparison": "same_progress",
        },
        history(date(2022, 12, 15), date(2024, 1, 10)),
        today=date(2024, 1, 10),
    )
    assert result["metadata"]["current_year"] == 2023
    assert historical(result, 2022)["actual_end"] == "2023-01-10"
    assert historical(result, 2023)["group"] == "current"
    assert result["effective_n"] == 1


def event(identifier, year, when, *, quarter=1, precision="before_open", verified=True):
    return {
        "id": identifier,
        "fiscal_year": year,
        "fiscal_quarter": quarter,
        "announced_date": when,
        "time_precision": precision,
        "verified": verified,
        "evidence": [
            {
                "provider": "synthetic", "source_url": "https://example.com/synthetic-announcement",
                "announced_date": when, "fiscal_year": year, "fiscal_quarter": quarter,
                "time_precision": precision,
                "time_evidence": f"Synthetic issuer states results actually released {precision}" if precision in {"before_open", "after_close"} else None,
                "note": "Synthetic independent date support",
            },
            {
                "provider": "synthetic", "source_url": "https://example.com/synthetic-quarterly-filing",
                "fiscal_year": year, "fiscal_quarter": quarter,
                "period_kind": "regular",
                "period_kind_evidence": "Synthetic ordinary fiscal quarter statement",
            },
        ],
    }


def test_earnings_window_numbering_independent_n_and_multiplicative_gap():
    prices = history(
        date(2023, 11, 1),
        date(2024, 1, 8),
        {
            "2023-12-29": "100",
            "2024-01-02": "121",
            "2024-01-08": "132",
        },
    )
    next(row for row in prices if row["date"] == "2024-01-02")["open"] = "110"
    result = compute_research(
        {
            "kind": "earnings",
            "years": [2023],
            "current_fiscal_year": 2024,
            "quarter": 1,
        },
        prices,
        [event("historical", 2023, "2024-01-02")],
        today=date(2024, 1, 8),
    )
    row = historical(result, 2023)
    assert row["reaction_date"] == "2024-01-02"
    assert row["baseline_date"] == "2023-12-29"
    assert row["opening_gap"] == "0.1"
    assert row["windows"]["1"]["after_open"] == "0.1"
    assert row["windows"]["1"]["cumulative"] == "0.21"
    assert row["windows"]["5"]["end_date"] == "2024-01-08"
    assert row["windows"]["5"]["cumulative"] == "0.32"
    assert row["windows"]["20"]["status"] == "not_yet_formed"
    assert row["windows"]["60"]["cumulative"] is None
    assert {key: value["n"] for key, value in result["summary"]["windows"].items()} == {
        "1": 1,
        "5": 1,
        "20": 0,
        "60": 0,
    }


@pytest.mark.parametrize("precision", ["date_only", "intraday", "conflict"])
def test_uncertain_earnings_dates_retain_observation_without_precise_statistics(precision):
    result = compute_research(
        {
            "kind": "earnings",
            "years": [2023],
            "current_fiscal_year": 2024,
        },
        history(date(2023, 11, 1), date(2024, 1, 8)),
        [event("a", 2023, "2024-01-02", precision=precision)],
        today=date(2024, 1, 8),
    )
    row = historical(result, 2023)
    assert row["endpoint"] == "0"
    assert row["opening_gap"] is None
    assert result["effective_n"] == 0
    assert result["series"][0]["points"]
    assert not result["summary"]["path"]


def test_earnings_missing_intermediate_price_keeps_endpoint_but_excludes_window():
    prices = [
        row for row in history(date(2023, 11, 1), date(2024, 1, 8)) if row["date"] != "2024-01-04"
    ]
    result = compute_research(
        {"kind": "earnings", "years": [2023], "current_fiscal_year": 2024},
        prices,
        [event("a", 2023, "2024-01-02")],
        today=date(2024, 1, 8),
    )
    assert result["summary"]["windows"]["1"]["n"] == 1
    assert result["summary"]["windows"]["5"]["n"] == 0
    assert historical(result, 2023)["windows"]["5"]["cumulative"] == "0"


def test_fiscal_years_are_company_specific_and_current_is_not_added_to_history():
    prices = history(date(2023, 8, 1), date(2024, 1, 31))
    events = [event("a", 2023, "2023-09-01"), event("b", 2024, "2024-01-02")]
    params = {"kind": "earnings", "historical_years": 1}
    september = compute_research(
        {**params, "fiscal_year_end_mmdd": "09-30"}, prices, events, today=date(2023, 10, 10)
    )
    december = compute_research(
        {**params, "fiscal_year_end_mmdd": "12-31"}, prices, events, today=date(2023, 10, 10)
    )
    assert september["metadata"]["current_year"] == 2024
    assert december["metadata"]["current_year"] == 2023
    assert september["metadata"]["historical_years"] == [2023]
    current = compute_research(
        {**params, "current_fiscal_year": 2024}, prices, events, today=date(2024, 1, 31)
    )
    assert current["effective_n"] == 1
    assert historical(current, 2024)["group"] == "current"


def test_unknown_fiscal_identity_and_duplicate_primary_events_are_explicit():
    prices = history(date(2023, 11, 1), date(2024, 1, 8))
    result = compute_research(
        {"kind": "earnings"}, prices, [event("a", 2023, "2024-01-02")], today=date(2024, 1, 8)
    )
    assert result["metadata"]["current_year"] is None
    assert result["metadata"]["warnings"] == ["current_fiscal_year_unconfirmed"]
    assert result["effective_n"] == 0
    duplicate = compute_research(
        {"kind": "earnings", "years": [2023], "current_fiscal_year": 2024},
        prices,
        [event("a", 2023, "2024-01-02"), event("b", 2023, "2024-01-03")],
        today=date(2024, 1, 8),
    )
    assert historical(duplicate, 2023)["status"] == "duplicate_primary_events"
    assert duplicate["effective_n"] == 0


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


def test_unqualified_bars_and_duplicate_versions_cannot_enter_research():
    params = {
        "kind": "interval",
        "years": [2023],
        "current_year": 2024,
        "start_mmdd": "01-03",
        "end_mmdd": "01-03",
    }
    result = compute_research(
        params, [bar("2023-01-03", status="UNVERIFIED")], today=date(2024, 2, 1)
    )
    assert result["effective_n"] == 0
    with pytest.raises(ValueError, match="Duplicate"):
        compute_research(
            params, [bar("2023-01-03"), bar("2023-01-03", "200")], today=date(2024, 2, 1)
        )


def test_earnings_scope_requires_actual_events_and_adds_exact_20_and_60_sessions():
    with pytest.raises(ValueError, match="Discover fiscal earnings"):
        plan_scope({"kind": "earnings", "historical_years": 8}, date(2024, 6, 1))
    start, end = plan_scope(
        {"kind": "earnings", "events": [event("a", 2023, "2024-01-02")]}, date(2024, 6, 1)
    )
    assert start == date(2023, 11, 30)
    assert end == date(2024, 3, 27)
    assert len(session_window(date(2023, 12, 29), 20, 60)) == 81
