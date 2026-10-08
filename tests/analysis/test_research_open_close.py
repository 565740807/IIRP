"""Monthly and interval research on the open → close basis (D6).

Two samples are hand-checked against MSFT daily bars copied from the local
24-hour cache on 2026-10-07 (data/msft_daily_sample.csv: Oct 2025 and
Dec 2024 – Jan 2025); the others are synthetic and hand-computed.
"""

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from iirp.analysis.calendar import sessions
from iirp.analysis.research import compute_research, plan_scope

SAMPLE = Path(__file__).with_name("data") / "msft_daily_sample.csv"


def msft_bars():
    with SAMPLE.open() as handle:
        return [{**row, "status": "VALID"} for row in csv.DictReader(handle)]


def bar(day, close="100", opening=None):
    return {"date": str(day), "open": opening or close, "high": max(close, opening or close, key=Decimal),
            "low": min(close, opening or close, key=Decimal), "close": close, "status": "VALID"}


def flat(start, end, overrides=None):
    """Flat 100 bars; ``overrides`` maps a date to (open, close)."""
    overrides = overrides or {}
    return [bar(day, *reversed(overrides.get(day.isoformat(), ("100", "100")))) for day in sessions(start, end)]


def period(result, key):
    return next(item for item in result["periods"] if item["key"] == key)


def year(item, value):
    return next(candle for candle in item["years"] if candle["year"] == value)


def test_msft_october_2025_is_first_open_to_last_close():
    result = compute_research({"kind": "monthly", "historical_years": 1, "current_year": 2026},
                              msft_bars(), today=date(2026, 10, 6))
    october = period(result, "10")
    candle = year(october, 2025)
    # Hand check: Oct 1 open 514.799987792969, Oct 31 close 517.809997558594,
    # 517.809997558594 / 514.799987792969 − 1 = +0.584695%; 23 sessions.
    assert (candle["start_date"], candle["end_date"]) == ("2025-10-01", "2025-10-31")
    assert (candle["open"], candle["close"]) == ("514.799987792969", "517.809997558594")
    assert (candle["high"], candle["low"]) == ("553.719970703125", "506.000000000000")
    assert candle["sessions"] == candle["expected_sessions"] == 23
    assert candle["status"] == "complete" and not candle["current"]
    assert round(Decimal(candle["change"]), 8) == Decimal("0.00584695")
    # 553.719970703125 / 514.799987792969 − 1 and 506 / 514.799987792969 − 1
    assert round(Decimal(candle["high_change"]), 6) == Decimal("0.075602")
    assert round(Decimal(candle["low_change"]), 6) == Decimal("-0.017094")
    stats = october["stats"]
    assert (stats["target_n"], stats["n"], stats["up"]) == (1, 1, 1)
    assert Decimal(stats["median"]) == Decimal(candle["change"]) and stats["best_year"] == 2025
    # This October has begun but its prices are not in the sample.
    assert year(october, 2026)["status"] == "no_data" and year(october, 2026)["current"]
    assert result["metadata"]["historical_years"] == [2025]


def test_msft_cross_year_interval_moves_both_ends_onto_sessions():
    # 2024-12-14 is a Saturday → start Monday 12-16 at the open; 2025-01-12 is a
    # Sunday → end Friday 01-10 at the close (01-09 was a market closure).
    params = {"kind": "interval", "historical_years": 2, "start_mmdd": "12-14", "end_mmdd": "01-12"}
    result = compute_research(params, msft_bars(), today=date(2026, 10, 6))
    item = period(result, "interval")
    assert item["cross_year"] and result["metadata"]["current_year"] == 2026
    candle = year(item, 2024)
    assert (candle["period_start"], candle["period_end"]) == ("2024-12-14", "2025-01-12")
    assert (candle["start_date"], candle["end_date"]) == ("2024-12-16", "2025-01-10")
    assert (candle["open"], candle["close"]) == ("447.269989013672", "418.950012207031")
    assert (candle["high"], candle["low"]) == ("455.290008544922", "414.850006103516")
    assert candle["sessions"] == candle["expected_sessions"] == 17
    # 418.950012207031 / 447.269989013672 − 1 = −6.331741%
    assert round(Decimal(candle["change"]), 8) == Decimal("-0.06331741")
    assert item["stats"]["n"] == 1 and item["stats"]["up"] == 0
    assert year(item, 2025)["status"] == "no_data"
    assert year(item, 2026)["status"] == "not_started"


def march_example():
    bars = []
    # (first session, its open) → (last session, its close) of each March.
    plan = {2020: ("2020-03-02", "100", "2020-03-31", "110"), 2021: ("2021-03-01", "100", "2021-03-31", "95"),
            2022: ("2022-03-01", "50", "2022-03-31", "52"), 2023: ("2023-03-01", "200", "2023-03-31", "202")}
    for first, opening, last, closing in plan.values():
        bars += flat(date.fromisoformat(first), date.fromisoformat(last),
                     {first: (opening, "100"), last: ("100", closing)})
    bars += flat(date(2024, 3, 1), date(2024, 3, 15), {"2024-03-01": ("100", "100"), "2024-03-15": ("100", "120")})
    return bars


def market_example():
    plan = {2020: ("2020-03-02", "100", "2020-03-31", "112"), 2021: ("2021-03-01", "100", "2021-03-31", "90"),
            2022: ("2022-03-01", "100", "2022-03-31", "101")}
    bars = []
    for first, opening, last, closing in plan.values():
        bars += flat(date.fromisoformat(first), date.fromisoformat(last),
                     {first: (opening, "100"), last: ("100", closing)})
    return bars


def test_statistics_excess_and_up_ratio_interval_hand_computed():
    params = {"kind": "monthly", "historical_years": 4, "current_year": 2024}
    benchmark = {"symbol": "^GSPC", "status": "available", "bars": market_example()}
    result = compute_research(params, march_example(), today=date(2024, 3, 15), benchmark=benchmark)
    march = period(result, "3")
    # Changes −5%, +1%, +4%, +10%.
    stats = march["stats"]
    assert (stats["n"], stats["up"], stats["flat"]) == (4, 3, 0)
    assert Decimal(stats["median"]) == Decimal("0.025") and Decimal(stats["mean"]) == Decimal("0.025")
    assert Decimal(stats["q25"]) == Decimal("-0.005") and Decimal(stats["q75"]) == Decimal("0.055")
    assert (Decimal(stats["best"]), stats["best_year"]) == (Decimal("0.1"), 2020)
    assert (Decimal(stats["worst"]), stats["worst_year"]) == (Decimal("-0.05"), 2021)
    # Wilson 95% for 3 of 4: 30.06%–95.44%, which contains one half.
    assert round(Decimal(stats["up_low"]), 4) == Decimal("0.3006")
    assert round(Decimal(stats["up_high"]), 4) == Decimal("0.9544")
    assert stats["coin_flip"] is True
    # Benchmark +12%, −10%, +1%; 2023 missing. Excess −2%, +5%, +3%.
    assert (stats["paired_n"], stats["beat"]) == (3, 2)
    assert Decimal(stats["median_excess"]) == Decimal("0.03")
    assert Decimal(stats["benchmark_median"]) == Decimal("0.01")
    first = year(march, 2020)
    assert (first["benchmark_open"], first["benchmark_close"]) == ("100", "112")
    assert Decimal(first["excess"]) == Decimal("-0.02")
    assert year(march, 2023)["benchmark_change"] is None and year(march, 2023)["excess"] is None
    # The current March is in progress and shown on its own, never in the statistics.
    current = year(march, 2024)
    assert (current["status"], current["current"], current["end_date"]) == ("in_progress", True, "2024-03-15")
    assert Decimal(current["change"]) == Decimal("0.2")
    assert result["metadata"]["benchmark"] == {"symbol": "^GSPC", "status": "available"}
    # April has not started this year and has no data in earlier years.
    assert year(period(result, "4"), 2024)["status"] == "not_started"
    assert period(result, "4")["stats"]["n"] == 0 and period(result, "4")["stats"]["coin_flip"] is None


def test_all_up_years_exclude_one_half_only_with_enough_samples():
    bars = []
    for value in range(2014, 2024):
        last = sessions(date(value, 3, 1), date(value, 3, 31))[-1]
        bars += flat(date(value, 3, 1), date(value, 3, 31), {last.isoformat(): ("100", "105")})
    result = compute_research({"kind": "monthly", "historical_years": 10, "current_year": 2024},
                              bars, today=date(2024, 2, 1))
    stats = period(result, "3")["stats"]
    assert (stats["n"], stats["up"], stats["up_high"]) == (10, 10, "1")
    assert Decimal(stats["up_low"]) > Decimal("0.5") and stats["coin_flip"] is False


def test_missing_first_open_or_last_close_is_excluded_not_zero():
    bars = flat(date(2022, 3, 1), date(2023, 3, 31))
    bars = [row for row in bars if row["date"] not in {"2022-03-01", "2023-03-31"}]
    # A missing interior session is listed but does not change open → close.
    bars = [row for row in bars if row["date"] != "2022-06-15"]
    result = compute_research({"kind": "monthly", "historical_years": 2, "current_year": 2024},
                              bars, today=date(2024, 1, 31))
    march = period(result, "3")
    assert [year(march, value)["status"] for value in (2022, 2023)] == ["incomplete", "incomplete"]
    assert year(march, 2022)["change"] is None and march["stats"]["n"] == 0
    june = year(period(result, "6"), 2022)
    assert june["status"] == "complete" and june["missing_dates"] == ["2022-06-15"]
    assert june["sessions"] == june["expected_sessions"] - 1


def test_unqualified_and_duplicate_bars_never_enter_research():
    params = {"kind": "interval", "historical_years": 1, "current_year": 2024,
              "start_mmdd": "01-03", "end_mmdd": "01-04"}
    rows = [{**bar("2023-01-03"), "status": "UNVERIFIED"}, bar("2023-01-04")]
    result = compute_research(params, rows, today=date(2024, 2, 1))
    assert year(period(result, "interval"), 2023)["status"] == "incomplete"
    with pytest.raises(ValueError, match="Duplicate"):
        compute_research(params, [bar("2023-01-03"), bar("2023-01-03", "200")], today=date(2024, 2, 1))


def test_feb_29_maps_to_feb_28_and_a_holiday_only_interval_has_no_sessions():
    params = {"kind": "interval", "historical_years": 2, "start_mmdd": "02-29", "end_mmdd": "03-01"}
    bars = flat(date(2022, 2, 1), date(2024, 3, 31))
    item = period(compute_research(params, bars, today=date(2024, 3, 31)), "interval")
    assert year(item, 2023)["period_start"] == "2023-02-28"
    assert year(item, 2024)["period_start"] == "2024-02-29"
    holiday = {"kind": "interval", "historical_years": 1, "start_mmdd": "07-04", "end_mmdd": "07-04"}
    candle = year(period(compute_research(holiday, bars, today=date(2024, 3, 31)), "interval"), 2023)
    assert candle["status"] == "no_data" and candle["change"] is None


def test_price_scope_covers_every_year_and_never_the_future():
    monthly = {"kind": "monthly", "current_year": 2026}
    assert plan_scope({**monthly, "historical_years": 8}, date(2026, 9, 1)) == (date(2018, 1, 1), date(2026, 9, 1))
    assert plan_scope({**monthly, "historical_years": 3}, date(2026, 9, 1))[0] == date(2023, 1, 1)
    # In January a cross-year interval's current year is the one that began last December.
    interval = {"kind": "interval", "historical_years": 1, "start_mmdd": "12-15", "end_mmdd": "01-20"}
    assert plan_scope(interval, date(2024, 1, 10)) == (date(2022, 12, 15), date(2024, 1, 10))
    result = compute_research(interval, [], today=date(2024, 1, 10))
    assert result["metadata"]["current_year"] == 2023 and result["metadata"]["historical_years"] == [2022]
    with pytest.raises(ValueError, match="historical_years"):
        plan_scope({**monthly, "historical_years": 0}, date(2026, 9, 1))


def test_a_running_period_ends_at_its_latest_close():
    # Today's bar after the close can come without a close price yet.
    bars = flat(date(2024, 3, 1), date(2024, 3, 14), {"2024-03-01": ("100", "100"), "2024-03-14": ("100", "110")})
    bars.append({"date": "2024-03-15", "open": "111", "high": "115", "low": "109", "close": None, "status": "MISSING_FIELDS"})
    result = compute_research({"kind": "monthly", "historical_years": 1, "current_year": 2024}, bars, today=date(2024, 3, 15))
    current = year(period(result, "3"), 2024)
    assert (current["status"], current["end_date"], current["close"]) == ("in_progress", "2024-03-14", "110")
    assert Decimal(current["change"]) == Decimal("0.1") and current["missing_dates"] == []
    # A finished period is never shortened: its missing last close leaves it incomplete.
    february = flat(date(2024, 2, 1), date(2024, 2, 28)) + [{"date": "2024-02-29", "open": "100", "high": "100",
                                                              "low": "100", "close": None, "status": "MISSING_FIELDS"}]
    done = compute_research({"kind": "monthly", "historical_years": 1, "current_year": 2024}, february, today=date(2024, 3, 15))
    assert year(period(done, "2"), 2024)["status"] == "incomplete"
