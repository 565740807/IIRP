"""Hand-computed distributions and missing-data counterexamples, no provider calls."""

from datetime import date
from decimal import Decimal

import pytest
from iirp.analytics.calendar import next_session
from iirp.analytics.distributions import statistics
from iirp.analytics.event_dates import analyze_event_dates
from iirp.analytics.research import compute_research
from test_event_dates import event, history
from test_research import history as price_history


def benchmark(bars, **extra):
    return {"symbol": "SYNTHETIC_ETF", "name": "开发合成ETF", "dataset_id": "synthetic-b1", "status": "available", "bars": bars, **extra}


def distribution(result, metric="after5", group="developer_keynote"):
    return next(d for d in result["distributions"] if d["metric"] == metric and d["group"] == group)


def test_full_statistics_hand_calculation_and_degenerate_samples():
    stats = statistics(["-.10", "0", ".10", ".90", None])
    expected = {"min": "-.10", "q25": "-.025", "median": ".05", "mean": ".225", "q75": ".30", "max": ".90"}
    assert stats["n"] == 4 and stats["up"] == 2
    for key, value in expected.items():
        assert Decimal(stats[key]) == Decimal(value)
    single = statistics([".11"])
    assert single["n"] == 1 and all(Decimal(single[k]) == Decimal(".11") for k in expected)
    empty = statistics([None])
    assert empty["n"] == 0 and all(empty[k] is None for k in expected)
    with pytest.raises(ValueError):
        statistics(["Infinity"])


def test_pair_individual_samples_before_median_and_same_n_three_distributions():
    events, stock, other = [], [], []
    for year, a, b in [(2021, "101", "101"), (2022, "101", "105"), (2023, "105", "105")]:
        day = f"{year}-06-10"
        events.append(event(day, client_event_id=str(year)))
        prices = [{**row, "close": "100"} for row in history(str(next_session(date.fromisoformat(day))))]
        stock.extend([{**row, "close": a if i == 11 else "100"} for i, row in enumerate(prices)])
        other.extend([{**row, "close": b if i == 11 else "100"} for i, row in enumerate(prices)])
    result = analyze_event_dates(events, stock, cutoff=date(2024, 1, 1), current_year=2024, benchmark=benchmark(other))
    d = distribution(result)
    assert [d[k]["n"] for k in ("stock", "paired_stock", "benchmark", "difference")] == [3, 3, 3, 3]
    assert Decimal(d["difference"]["median"]) == 0
    assert Decimal(d["paired_stock"]["median"]) - Decimal(d["benchmark"]["median"]) == Decimal("-.04")
    assert all(s["start_date"] != s["end_date"] and s["paired"] for s in d["samples"])


def test_benchmark_hole_and_inception_exclude_pairs_without_remapping_stock():
    bars = history()
    missing = bars[8]["date"]
    result = analyze_event_dates([event()], bars, cutoff=date(2024, 12, 31), current_year=2025,
                                 benchmark=benchmark([row for row in bars if row["date"] != missing]))
    after = distribution(result)
    assert after["stock"]["n"] == 1 and after["benchmark"]["n"] == 0
    assert after["samples"][0]["end_date"] == bars[-1]["date"]
    assert after["missing"] == {"benchmark_missing_prices": 1}
    assert distribution(result, "before5")["benchmark"]["n"] == 1
    result = analyze_event_dates([event()], bars, cutoff=date(2024, 12, 31), current_year=2025,
                                 benchmark=benchmark(bars, listing_date="2024-06-11"))
    assert distribution(result)["missing"] == {"before_listing": 1}


def test_frozen_cutoff_masks_future_benchmark_points_and_immature_windows():
    bars = history()
    result = analyze_event_dates([event()], bars, cutoff=date.fromisoformat(bars[7]["date"]), current_year=2025, benchmark=benchmark(bars))
    for point in result["series"][0]["points"]:
        if point["x"] > 1:
            assert point["value"] is None and point["benchmark"] is None and point["difference"] is None
    sample = distribution(result)["samples"][0]
    assert sample["benchmark"] is None and sample["benchmark_status"] == "not_yet_formed"


def test_current_observation_difference_is_visible_without_increasing_history_n():
    bars = history()
    other = [{**b, "close": "100"} for b in bars]
    result = analyze_event_dates([event()], bars, cutoff=date(2024, 12, 31), current_year=2024, benchmark=benchmark(other))
    d = distribution(result)
    assert d["stock"]["n"] == d["difference"]["n"] == 0
    assert Decimal(d["samples"][0]["difference"]) == Decimal(".1")
    assert not d["samples"][0]["paired"]


def test_fiscal_review_gaps_and_all_missing_eight_years_never_report_available():
    fact = event(event_type="earnings_release", fiscal_year=2024, fiscal_quarter=1, period_kind="regular", period_verified=False)
    result = analyze_event_dates([fact], history(), cutoff=date(2024, 12, 31), current_year=2025, current_fiscal_year=2025,
                                 metadata={"research_kind": "earnings", "requested_fiscal_years": list(range(2017, 2025))})
    d = distribution(result, group="Q1")
    assert d["stock"]["n"] == 0 and "available" not in d["missing"]
    assert next(g for g in result["fiscal_coverage"]["gaps"] if g["year"] == 2024 and g["quarter"] == "Q1")["status"] == "unverified_or_nonstandard_fiscal_period"
    empty = analyze_event_dates([], [], cutoff=date(2024, 12, 31), current_year=2025, current_fiscal_year=2025,
                                metadata={"research_kind": "earnings", "requested_fiscal_years": list(range(2017, 2025))})
    assert len(empty["fiscal_coverage"]["gaps"]) == 32
    assert all(r["statistics"]["n"] == 0 and len(r["target_years"]) == 8 for r in empty["fiscal_coverage"]["rankings"])


def test_common_quarter_years_apply_to_windows_and_paths_but_not_custom_events():
    events, bars = [], []
    for year in (2022, 2023):
        for quarter, month in enumerate((2, 5, 8, 11), 1):
            if year == 2023 and quarter == 4:
                continue
            day = f"{year}-{month:02}-10"
            events.append(event(day, client_event_id=f"{year}-{quarter}", event_type="earnings_release", fiscal_year=year, fiscal_quarter=quarter, period_verified=True, period_kind="regular"))
            bars.extend(history(day))
    result = analyze_event_dates(events, bars, cutoff=date(2024, 1, 1), current_year=2024, current_fiscal_year=2024, metadata={"common_years": True})
    assert all(d["stock"]["n"] == 1 for d in result["distributions"])
    assert all(p["n"] == 1 for category in result["category_summary"] for p in category["path"])
    custom = analyze_event_dates([event()], history(), cutoff=date(2024, 12, 31), current_year=2025, metadata={"common_years": True})
    assert distribution(custom)["stock"]["n"] == 1


@pytest.mark.parametrize("kind", ["monthly", "interval", "earnings"])
def test_all_research_modes_serialize_statistics_benchmarks_and_actual_windows(kind):
    params = {"kind": kind, "current_year": 2024, "current_fiscal_year": 2024, "years": [2023], "month": 1, "comparison": "complete", "start_mmdd": "01-03", "end_mmdd": "01-10"}
    bars = price_history(date(2022, 12, 1), date(2024, 6, 30))
    events = [{"id": "synthetic", "fiscal_year": 2023, "fiscal_quarter": 1, "announced_date": "2023-01-03", "announced_at": "2023-01-03T08:00:00-05:00", "time_precision": "exact", "verified": True,
               "evidence": [{"source_url": "https://example.com/regular-2023-q1", "announced_date": "2023-01-03", "fiscal_year": 2023, "fiscal_quarter": 1,
                             "period_kind": "regular", "period_kind_evidence": "Synthetic quarterly source explicitly says regular.",
                             "announced_at": "2023-01-03T08:00:00-05:00", "time_precision": "exact",
                             "time_evidence": "Synthetic issuer statement: results actually released January 3 at 8:00 a.m. ET"}]}]
    result = compute_research(params, bars, events, today=date(2024, 6, 30), benchmark=benchmark(bars))
    d = distribution(result, "5" if kind == "earnings" else "endpoint", "Q1" if kind == "earnings" else "1" if kind == "monthly" else "interval")
    assert d["stock"]["n"] == d["difference"]["n"] == 1
    assert Decimal(d["difference"]["median"]) == 0
    assert d["samples"][0]["start_date"] <= d["samples"][0]["end_date"]
    assert result["benchmark"]["dataset_id"] == "synthetic-b1"
