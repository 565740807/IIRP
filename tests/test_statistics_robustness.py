"""Independent hand values and counterexamples; pure synthetic functions, no DB."""
from datetime import date
from decimal import Decimal

import pytest
from iirp.analytics.distributions import Distribution, add_distributions
from iirp.analytics.research import compute_research
from test_research import history as price_history


def direct(values, years=None, target=None):
    years = list(range(2018, 2018 + len(values))) if years is None else years
    rows = [{"key": str(year), "year": year, "group": "historical", "endpoint": value,
             "eligible": value is not None, "complete": value is not None, "period_ended": True,
             "status": "available" if value is not None else "incomplete_path",
             "baseline_date": f"{year}-01-02", "actual_end": f"{year}-01-31"}
            for year, value in zip(years, values, strict=True)]
    return add_distributions({"kind": "interval", "metadata": {"cutoff_date": "2026-09-21", "calendar": "XNYS",
                             "historical_years": target if target is not None else years},
                              "rows": rows, "series": []})["distributions"][0]


def test_hand_computed_leave_one_out_and_single_outlier_influence():
    item = direct(["-.10", "0", ".10", ".90"])
    robust = item["robustness"]
    assert robust["sample_unit"] == "annual_sample" and robust["n"] == 4
    assert robust["sample_keys"] == ["2018", "2019", "2020", "2021"]
    sensitivity = robust["leave_one_out"]
    assert sensitivity["available"]
    expected_means = [Decimal(1) / 3, Decimal(".3"), Decimal(4) / 15, Decimal(0)]
    expected_medians = [Decimal(".1"), Decimal(".1"), Decimal(0), Decimal(0)]
    for point, mean, median in zip(sensitivity["points"], expected_means, expected_medians, strict=True):
        assert point["n"] == 3
        assert abs(Decimal(point["mean"]) - mean) < Decimal("1e-27")
        assert Decimal(point["median"]) == median
    assert Decimal(sensitivity["mean_min"]) == 0
    assert abs(Decimal(sensitivity["mean_max"]) - Decimal(1) / 3) < Decimal("1e-27")
    assert sensitivity["influential_sample_keys"] == ["2021"]
    assert Decimal(sensitivity["max_abs_mean_change"]) == Decimal(".225")
    assert "不是样本外" in sensitivity["interpretation"]


@pytest.mark.parametrize("values,lower,upper", [
    ([".1"] * 8, ".6755924351161198", "1"),
    (["-.1"] * 8, "0", ".3244075648838801"),
    (["-.1", "0", ".1", ".9"], ".15003898915214958", ".8499610108478504"),
    ([".1"], ".20654931437723745", "1"),
])
def test_wilson_matches_independent_normal_reference(values, lower, upper):
    result = direct(values)["robustness"]["proportion"]
    assert result["method"] == "wilson_score" and result["confidence_level"] == "0.95"
    assert abs(Decimal(result["lower"]) - Decimal(lower)) < Decimal("1e-14")
    assert abs(Decimal(result["upper"]) - Decimal(upper)) < Decimal("1e-14")
    assert result["flat"] == values.count("0")
    assert "独立" in result["assumptions"] and "不是未来" in result["interpretation"]


def test_empty_and_single_samples_do_not_invent_stability_or_zero_returns():
    empty = direct([None])["robustness"]
    assert empty["n"] == 0 and empty["proportion"]["lower"] is None
    assert not empty["leave_one_out"]["available"] and not empty["leave_one_out"]["points"]
    single = direct([".7"])["robustness"]
    assert not single["leave_one_out"]["available"]
    assert single["leave_one_out"]["points"][0]["n"] == 0
    assert single["leave_one_out"]["points"][0]["mean"] is None


def test_early_late_boundary_uses_target_year_span_not_survivors_or_returns():
    target = list(range(2018, 2026))
    item = direct([None, None, None, ".9", "-.2", "-.1", ".1", ".2"], target=target)
    segment = item["robustness"]["historical_segments"]
    assert segment["policy"] == "target_year_midpoint" and segment["boundary_year"] == 2021
    assert segment["earlier"]["target_years"] == [2018, 2019, 2020, 2021]
    assert segment["earlier"]["statistics"]["n"] == 1
    assert segment["later"]["statistics"]["n"] == 4
    reversed_item = direct([".1", "-.1", ".2", "-.2", None, None, None, ".9"], target=target)
    assert reversed_item["robustness"]["historical_segments"]["boundary_year"] == 2021


def test_monthly_and_interval_baseline_and_drawdown_are_independently_reproducible():
    bars = price_history(date(2022, 12, 1), date(2024, 1, 31), {"2023-01-03": "110", "2023-01-04": "120", "2023-01-05": "90", "2023-01-31": "121"})
    params = {"years": [2023], "current_year": 2024, "month": 1, "comparison": "complete"}
    monthly = compute_research({**params, "kind": "monthly"}, bars, today=date(2024, 1, 31))
    interval = compute_research({**params, "kind": "interval", "start_mmdd": "01-01", "end_mmdd": "01-31"}, bars, today=date(2024, 1, 31))
    assert Decimal(monthly["rows"][0]["endpoint"]) == Decimal(".21")
    assert Decimal(interval["rows"][0]["endpoint"]) == Decimal(".1")
    assert Decimal(monthly["distributions"][0]["closing_max_drawdown"]["max"]) == Decimal(".25")
    assert "上月最后" in monthly["metadata"]["methodology"]["baseline_rule"]
    assert "首个交易日收盘" in interval["metadata"]["methodology"]["baseline_rule"]


def test_old_distribution_remains_readable_without_inventing_robustness():
    old = Distribution.model_validate({"key": "1:endpoint", "label": "1月", "metric": "endpoint", "group": "1", "stock": {"n": 1, "mean": ".1"}})
    assert old.robustness is None and old.closing_max_drawdown is None


