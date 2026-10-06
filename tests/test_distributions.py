"""Hand-computed distributions and missing-data counterexamples, no provider calls."""

from datetime import date
from decimal import Decimal

import pytest
from iirp.analytics.distributions import statistics
from iirp.analytics.research import compute_research
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


@pytest.mark.parametrize("kind", ["monthly", "interval"])
def test_all_research_modes_serialize_statistics_benchmarks_and_actual_windows(kind):
    params = {"kind": kind, "current_year": 2024, "years": [2023], "month": 1, "comparison": "complete", "start_mmdd": "01-03", "end_mmdd": "01-10"}
    bars = price_history(date(2022, 12, 1), date(2024, 6, 30))
    result = compute_research(params, bars, today=date(2024, 6, 30), benchmark=benchmark(bars))
    d = distribution(result, "endpoint", "1" if kind == "monthly" else "interval")
    assert d["stock"]["n"] == d["difference"]["n"] == 1
    assert Decimal(d["difference"]["median"]) == 0
    assert d["samples"][0]["start_date"] <= d["samples"][0]["end_date"]
    assert result["benchmark"]["dataset_id"] == "synthetic-b1"
