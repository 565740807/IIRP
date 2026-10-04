"""Hand-computed event windows; synthetic prices, no database or source requests."""

from datetime import date
from decimal import Decimal

import pytest
from iirp.analytics.calendar import reaction_session, session_window
from iirp.analytics.event_dates import (
    analyze_event_dates,
    event_price_scope,
    resolve_event_anchor,
)


def event(day="2024-06-10", **extra):
    return {
        "client_event_id": "event-1",
        "event_name": "Synthetic keynote",
        "event_type": "developer_keynote",
        "event_year": int(day[:4]),
        "event_date": day,
        "event_time": None,
        "timezone": None,
        "time_precision": "date",
        "time_basis": "unknown",
        "event_status": "occurred",
        "date_status": "supported",
        "date_verified": True,
        "time_verified": False,
        **extra,
    }


def history(anchor="2024-06-10"):
    days = session_window(date.fromisoformat(anchor), 6, 5)
    # D-6=100, D-1=110, D0=99, D+5=108.9; all other closes are qualified.
    prices = ["100", "102", "104", "106", "108", "110", "99", "100", "102", "104", "106", "108.9"]
    return [
        {"date": str(day), "close": close, "status": "VALID"}
        for day, close in zip(days, prices, strict=True)
    ]


def calculate(events=None, bars=None, **options):
    return analyze_event_dates(
        events or [event()],
        history() if bars is None else bars,
        **{"cutoff": date(2024, 12, 31), "current_year": 2025, **options},
    )


def test_hand_calculated_five_day_returns_use_d_minus_6_and_d0_is_separate():
    result = calculate()
    row = result["rows"][0]
    assert len(row["points"]) == 11
    assert [point["x"] for point in row["points"]] == list(range(-5, 6))
    assert row["baseline_extra_close"] == "100"
    expected = {"before5": "0.1", "day0": "-0.1", "after5": "0.1", "through5": "-0.01"}
    for name, value in expected.items():
        window = row["windows"][name]
        assert Decimal(window["value"]) == Decimal(value)
        assert window["complete"] and window["mature"] and window["eligible"]
    assert Decimal(row["points"][0]["daily_return"]) == Decimal("0.02")
    assert Decimal(row["points"][4]["value"]) == 0
    assert result["effective_n"] == 1
    assert result["category_summary"][0]["windows"]["after5"]["n"] == 1
    assert result["series"][0]["points"] == row["points"]
    assert result["cells"][0]["date"] == row["points"][0]["date"]


@pytest.mark.parametrize(
    "window,first,last,expected,benchmark_numerator,benchmark_denominator",
    [
        ("before5", -6, -1, "0.1", 1, 10),
        ("day0", -1, 0, "-0.1", 1, 55),
        ("after5", 0, 5, "0.1", 5, 56),
        ("through5", -1, 5, "-0.01", 6, 55),
    ],
)
def test_selected_window_chart_statistics_and_paired_endpoints_agree(
    window, first, last, expected, benchmark_numerator, benchmark_denominator
):
    benchmark = {
        "symbol": "^GSPC",
        "status": "available",
        "bars": [{**bar, "close": str(100 + 2 * index)} for index, bar in enumerate(history())],
    }
    result = calculate(metadata={"params": {"date_window": window}}, benchmark=benchmark)
    row = result["rows"][0]
    points = result["window_series"][0]["points"]
    assert [point["x"] for point in points] == list(range(first, last + 1))
    assert Decimal(points[0]["value"]) == 0
    assert Decimal(points[-1]["value"]) == Decimal(expected)
    expected_benchmark = Decimal(benchmark_numerator) / Decimal(benchmark_denominator)
    assert abs(Decimal(points[-1]["benchmark"]) - expected_benchmark) < Decimal("1e-25")
    assert abs(
        Decimal(points[-1]["difference"]) - (Decimal(expected) - expected_benchmark)
    ) < Decimal("1e-25")
    assert points[-1]["benchmark"] == row["benchmark_windows"][window]["benchmark"]
    sample = next(d for d in result["distributions"] if d["metric"] == window)
    assert sample["stock"]["n"] == sample["difference"]["n"] == 1
    assert Decimal(sample["stock"]["median"]) == Decimal(points[-1]["value"])
    assert sample["samples"][0]["start_date"] == points[0]["date"]
    assert sample["samples"][0]["end_date"] == points[-1]["date"]
    assert row["selected_window_eligible"]
    assert result["category_summary"][0]["window_path"][-1]["n"] == 1


def test_selected_window_n_does_not_require_unselected_window_prices_or_count_current():
    result = calculate(bars=history()[1:], metadata={"params": {"date_window": "after5"}})
    assert not result["rows"][0]["eligible"]  # Complete full-evidence window still has its gap.
    assert result["rows"][0]["selected_window_eligible"]
    assert result["category_summary"][0]["window_path"][-1]["n"] == 1
    current = calculate(current_year=2024, metadata={"params": {"date_window": "after5"}})
    assert current["window_series"][0]["points"][-1]["value"] is not None
    assert current["category_summary"][0]["window_path"][-1]["n"] == 0
    missing = history()
    del missing[8]
    partial = calculate(bars=missing, metadata={"params": {"date_window": "after5"}})
    assert partial["window_series"][0]["points"][2]["value"] is None
    assert partial["category_summary"][0]["window_path"][-1]["n"] == 0


def test_missing_intermediate_price_keeps_calendar_positions_and_excludes_only_affected_window():
    bars = history()
    missing_date = bars[8]["date"]  # D+2
    del bars[8]
    result = calculate(bars=bars)
    row = result["rows"][0]
    assert row["points"][7]["date"] == missing_date
    assert row["points"][7]["close"] is None
    assert row["windows"]["before5"]["eligible"]
    assert row["windows"]["day0"]["eligible"]
    after = row["windows"]["after5"]
    assert Decimal(after["value"]) == Decimal("0.1")  # Endpoints may be a partial observation.
    assert after["status"] == "incomplete_path"
    assert not after["complete"] and not after["eligible"]
    assert result["effective_n"] == 0
    assert result["category_summary"][0]["windows"]["day0"]["n"] == 1
    assert result["category_summary"][0]["windows"]["after5"]["n"] == 0
    assert result["category_summary"][0]["path"][10]["n"] == 0


def test_future_prices_do_not_count_even_if_a_future_bar_was_supplied():
    cutoff = date.fromisoformat(history()[8]["date"])  # D+2
    result = calculate(cutoff=cutoff)
    row = result["rows"][0]
    assert row["windows"]["before5"]["eligible"]
    assert row["windows"]["after5"]["status"] == "not_yet_formed"
    assert row["windows"]["after5"]["value"] is None
    assert row["points"][-1]["value"] is None
    assert row["points"][-1]["status"] == "not_yet_formed"
    assert result["metadata"]["cutoff_date"] == str(cutoff)


def test_missing_d_minus_6_does_not_turn_four_returns_into_before_five():
    result = calculate(bars=history()[1:])
    row = result["rows"][0]
    assert row["windows"]["before5"]["value"] is None
    assert not row["windows"]["before5"]["eligible"]
    assert row["windows"]["day0"]["eligible"]
    assert row["windows"]["after5"]["eligible"]


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"date_verified": False}, "unverified_event_date"),
        ({"event_status": "scheduled"}, "event_not_confirmed_occurred"),
        ({"event_status": "unknown"}, "event_not_confirmed_occurred"),
        ({"event_status": "cancelled"}, "event_not_confirmed_occurred"),
        ({"excluded": True}, "user_excluded"),
        ({"date_status": "unverified"}, "unsupported_event_date"),
    ],
)
def test_complete_prices_cannot_replace_review_or_occurrence(overrides, reason):
    result = calculate([event(**overrides)])
    row = result["rows"][0]
    assert row["windows"]["after5"]["complete"]
    assert not row["windows"]["after5"]["eligible"]
    assert reason in row["exclusion_reasons"]
    assert result["effective_n"] == 0


def test_only_date_verified_events_are_eligible_for_date_observations_without_minute():
    result = calculate([event()])
    assert result["effective_n"] == 1
    assert result["category_summary"][0]["date_only_n"] == 1
    anchor = result["rows"][0]["anchor"]
    assert anchor["date_alignment_approximate"]
    assert anchor["release_session"] == "unknown"


@pytest.mark.parametrize(
    "day,anchor",
    [
        ("2024-06-08", "2024-06-10"),
        ("2024-03-29", "2024-04-01"),  # Good Friday
        ("2024-07-04", "2024-07-05"),
    ],
)
def test_weekends_and_holidays_keep_original_date_and_shift_only_d0(day, anchor):
    result = resolve_event_anchor(event(day))
    assert result["original_date"] == day
    assert result["exchange_date"] == day
    assert result["anchor_date"] == anchor
    assert result["non_session_shift"]


@pytest.mark.parametrize(
    "clock,session",
    [
        ("08:00", "before_open"),
        ("09:30", "during_session"),
        ("12:59", "during_session"),
        ("13:00", "after_close"),
        ("16:01", "after_close"),
    ],
)
def test_actual_minutes_use_early_close_but_after_close_preserves_date_d0(clock, session):
    row = event(
        "2024-11-29",
        event_time=clock,
        timezone="America/New_York",
        time_precision="minute",
        time_basis="reported_actual",
        time_verified=True,
    )
    anchor = resolve_event_anchor(row)
    assert anchor["anchor_date"] == "2024-11-29"
    assert anchor["release_session"] == session
    if session == "after_close":
        assert anchor["first_subsequent_session"] == "2024-12-02"
        assert any("D+1" in warning for warning in anchor["warnings"])


def test_official_schedule_does_not_become_actual_release_time():
    row = event(
        event_time="17:00",
        timezone="America/New_York",
        time_precision="minute",
        time_basis="official_schedule",
        time_verified=True,
    )
    anchor = resolve_event_anchor(row)
    assert anchor["release_session"] == "unknown"
    assert anchor["scheduled_session"] == "after_close"
    assert anchor["anchor_date"] == "2024-06-10"


def test_cross_timezone_date_conversion_and_date_only_caution():
    anchor = resolve_event_anchor(
        event(
            "2024-07-03",
            event_time="01:00",
            timezone="Asia/Tokyo",
            time_precision="minute",
            time_basis="reported_actual",
            time_verified=True,
        )
    )
    assert anchor["original_date"] == "2024-07-03"
    assert anchor["exchange_date"] == "2024-07-02"
    assert anchor["anchor_date"] == "2024-07-02"
    approximate = resolve_event_anchor(event("2024-07-03", timezone="Asia/Tokyo"))
    assert approximate["exchange_date"] == "2024-07-03"
    assert approximate["date_alignment_approximate"]
    assert any("跨日" in message for message in approximate["warnings"])


def test_evidence_backed_after_close_session_works_without_inventing_minute():
    row = event(release_session="after_close", time_verified=True)
    anchor = resolve_event_anchor(row)
    assert anchor["release_session"] == "after_close"
    assert anchor["anchor_date"] == "2024-06-10"
    assert anchor["first_subsequent_session"] == "2024-06-11"
    assert row["event_time"] is None


def test_date_d0_and_precise_reaction_numbering_have_different_endpoints_before_open():
    row = event(release_session="before_open", time_verified=True)
    observed = resolve_event_anchor(row)
    reaction = reaction_session(
        None, announced_date=row["event_date"], time_precision="before_open"
    )
    assert observed["anchor_date"] == reaction["reaction_date"]
    d_plus_5 = session_window(date.fromisoformat(observed["anchor_date"]), 0, 5)[-1]
    r_day_5 = session_window(date.fromisoformat(reaction["reaction_date"]), 0, 4)[-1]
    assert d_plus_5 != r_day_5


def test_type_counts_are_event_weighted_and_same_year_events_are_not_mislabeled_as_years():
    selected = [
        event(client_event_id="one"),
        event(client_event_id="two"),
        event(client_event_id="three"),
        event(client_event_id="product", event_type="product_launch"),
    ]
    result = calculate(selected)
    groups = {group["category"]: group for group in result["category_summary"]}
    assert groups["developer_keynote"]["event_count"] == 3
    assert groups["developer_keynote"]["year_count"] == 1
    assert groups["developer_keynote"]["windows"]["after5"]["n"] == 3
    assert groups["product_launch"]["windows"]["after5"]["n"] == 1
    assert result["rows"][0]["overlap"]["preview_event_ids"] == ["two", "three", "product"]
    assert result["rows"][0]["overlap"]["total"] == 3


def test_current_year_can_show_complete_windows_without_entering_history_n():
    result = calculate(current_year=2024)
    assert result["rows"][0]["group"] == "current"
    assert result["rows"][0]["windows"]["after5"]["complete"]
    assert result["effective_n"] == 0
    assert result["category_summary"][0]["windows"]["after5"]["n"] == 0


def test_fiscal_grouping_uses_fiscal_year_not_release_natural_year():
    row = event(
        event_type="earnings_release",
        fiscal_year=2023,
        fiscal_quarter=4,
        period_kind="regular",
        period_verified=True,
        period_end="2023-12-31",
    )
    result = calculate([row], current_year=2024, current_fiscal_year=2024)
    assert result["rows"][0]["year"] == 2023
    assert result["rows"][0]["event_year"] == 2024
    assert result["rows"][0]["group"] == "historical"
    assert result["category_summary"][0]["category"] == "Q4"
    assert result["effective_n"] == 1
    current = calculate([row], current_fiscal_year=2023)
    assert current["rows"][0]["group"] == "current"
    assert current["effective_n"] == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"period_verified": False},
        {"fiscal_quarter": None},
        {"fiscal_year": None},
        {"period_kind": "transition"},
        {"period_kind": "unknown"},
    ],
)
def test_unassigned_or_unreviewed_period_never_enters_standard_quarter_summary(overrides):
    row = event(
        event_type="earnings_release",
        fiscal_year=2023,
        fiscal_quarter=4,
        period_kind="regular",
        period_verified=True,
    )
    row.update(overrides)
    result = calculate([row], current_fiscal_year=2024)
    assert result["effective_n"] == 0
    assert "unverified_or_nonstandard_fiscal_period" in result["rows"][0]["exclusion_reasons"]


def test_before_listing_and_unverified_prices_are_distinguished_from_future():
    row = calculate(metadata={"listing_date": "2024-06-10"})["rows"][0]
    assert row["windows"]["before5"]["status"] == "before_listing"
    bars = history()
    bars[6]["status"] = "UNVERIFIED"
    other = calculate(bars=bars)["rows"][0]
    assert other["windows"]["day0"]["value"] is None
    assert other["windows"]["day0"]["status"] == "incomplete_path"


def test_scope_merges_overlapping_windows_not_unrelated_eight_year_history():
    selection = [
        event(),
        event("2024-06-11", client_event_id="next"),
        event("2023-06-10", client_event_id="older"),
    ]
    ranges = event_price_scope(selection, date(2024, 6, 12))
    assert len(ranges) == 2
    assert ranges[-1][0] == date.fromisoformat(history()[0]["date"])
    assert ranges[-1][1] == date(2024, 6, 12)
    assert event_price_scope([event(excluded=True)], date(2025, 1, 1)) == []
    assert event_price_scope([event(date_status="conflicting")], date(2025, 1, 1)) == []


def test_duplicate_price_versions_and_wrong_basis_are_rejected():
    with pytest.raises(ValueError, match="Duplicate daily bar"):
        calculate(bars=[*history(), history()[0]])
    with pytest.raises(ValueError, match="split_only"):
        calculate(metadata={"price_basis": "total_return"})
    with pytest.raises(ValueError, match="unique"):
        calculate([event(), event()])


def test_empty_or_conflicting_date_does_not_produce_fictional_positions():
    row = event(event_date=None, date_status="conflicting")
    result = calculate([row])
    assert result["rows"][0]["points"] == []
    assert result["effective_n"] == 0
    assert result["category_summary"][0]["windows"]["after5"]["n"] == 0


def test_unresolved_duplicate_fiscal_period_does_not_double_historical_n():
    first = event(
        event_type="earnings_release",
        fiscal_year=2023,
        fiscal_quarter=4,
        period_kind="regular",
        period_verified=True,
    )
    second = {**first, "client_event_id": "possible-duplicate"}
    result = calculate([first, second], current_fiscal_year=2024)
    assert result["effective_n"] == 0
    assert all("duplicate_fiscal_period" in row["exclusion_reasons"] for row in result["rows"])
    second["excluded"] = True
    resolved = calculate([first, second], current_fiscal_year=2024)
    assert resolved["effective_n"] == 1


def test_verified_minute_conflicting_with_claimed_session_does_not_claim_actual_timing():
    row = event(
        event_time="08:00",
        timezone="America/New_York",
        time_precision="minute",
        time_basis="reported_actual",
        time_verified=True,
        release_session="after_close",
    )
    anchor = resolve_event_anchor(row)
    assert anchor["release_session"] == "unknown"
    assert any("不一致" in warning for warning in anchor["warnings"])


def test_old_imported_regular_review_without_period_kind_source_cannot_enter_n():
    row = event(
        event_type="earnings_release", fiscal_year=2023, fiscal_quarter=4,
        period_kind="regular", period_verified=True,
        sources=[{"supports": ["fiscal_year", "fiscal_quarter", "event_date"]}],
    )
    result = calculate([row], current_fiscal_year=2024, metadata={"event_set_id": "legacy-import"})
    assert result["effective_n"] == 0
    assert "unverified_or_nonstandard_fiscal_period" in result["rows"][0]["exclusion_reasons"]
    row["sources"][0]["supports"].append("period_kind")
    qualified = calculate([row], current_fiscal_year=2024, metadata={"event_set_id": "reviewed-import"})
    assert qualified["effective_n"] == 1


def test_q2_only_common_years_keeps_qualified_sample_and_path():
    row = event(
        event_type="earnings_release", fiscal_year=2024, fiscal_quarter=2,
        period_kind="regular", period_verified=True,
        sources=[{"supports": ["period_kind"]}],
    )
    result = calculate(
        [row], current_fiscal_year=2025,
        metadata={"event_set_id": "q2-only", "common_years": True,
                  "requested_fiscal_quarters": [2], "historical_years": [2024]},
    )
    assert result["effective_n"] == 1
    assert result["rows"][0]["windows"]["after5"]["eligible"] is True
    assert any(point["eligible"] for point in result["rows"][0]["points"])
    assert {item["quarter"] for item in result["fiscal_coverage"]["rankings"]} == {"Q2"}
