"""Annual month ranking: full historical windows, missing data, ties and pairing."""
from datetime import date
from decimal import Decimal

from iirp.analysis.distributions import monthly_rankings, statistics
from iirp.analysis.research import compute_research

from tests.analysis.test_research import history


def test_full_twelve_months_independent_of_focused_same_progress_path():
    bars = history(date(2021, 12, 1), date(2024, 9, 15), {"2023-09-29": "120"})
    params = {"kind": "monthly", "current_year": 2024, "years": [2022, 2023], "month": 9, "comparison": "same_progress"}
    result = compute_research(params, bars, today=date(2024, 9, 15))
    complete = compute_research({**params, "month": 12, "comparison": "complete"}, bars, today=date(2024, 9, 15))
    assert result["distributions"] == complete["distributions"]
    assert result["monthly_rankings"] == complete["monthly_rankings"]
    assert len(result["monthly_rankings"]) == 12
    assert [d["stock"]["n"] for d in result["distributions"]] == [2] * 12
    september = next(d for d in result["distributions"] if d["group"] == "9")
    assert Decimal(september["stock"]["mean"]) == Decimal(".1")
    assert Decimal(result["summary"]["mean"]) == 0  # focused half-month path stays separate
    assert all(not s["eligible"] for d in result["distributions"] for s in d["samples"] if s["group"] == "current")
    assert next(c for c in result["cells"] if c["year"] == 2024 and c["month"] == 9)["status"] == "in_progress"


def test_monthly_empty_single_and_benchmark_hole_do_not_fill_zero_or_change_stock_rank():
    bars = history(date(2022, 12, 1), date(2023, 12, 31))
    bars = [b for b in bars if b["date"] != "2023-02-15"]
    other = [b for b in bars if b["date"] != "2023-03-15"]
    params = {"kind": "monthly", "current_year": 2024, "years": [2023], "month": 9}
    result = compute_research(params, bars, today=date(2024, 9, 15), benchmark={"symbol": "SYNTHETIC_ETF", "status": "available", "bars": other})
    feb = next(d for d in result["distributions"] if d["group"] == "2")
    march = next(d for d in result["distributions"] if d["group"] == "3")
    assert feb["stock"]["n"] == 0 and feb["stock"]["mean"] is None
    assert feb["missing"]["incomplete_path"] == 1
    assert march["stock"]["n"] == 1
    assert march["paired_stock"]["n"] == march["benchmark"]["n"] == march["difference"]["n"] == 0
    assert result["monthly_rankings"][1]["mean_rank"] is None
    assert result["monthly_rankings"] == compute_research(params, bars, today=date(2024, 9, 15))["monthly_rankings"]


def test_mean_median_ranks_differ_and_ties_use_dense_ranks_in_both_directions():
    values = [["0", "0", ".9"], [".2", ".2", ".2"], [".2", ".2", ".2"], []]
    ranks = monthly_rankings([{"group": str(i + 1), "stock": statistics(v), "years": [2021, 2022, 2023] if v else [], "target_years": [2021, 2022, 2023]} for i, v in enumerate(values)])
    assert [r["mean_rank"] for r in ranks] == [1, 2, 2, None]
    assert [r["median_rank"] for r in ranks] == [2, 1, 1, None]
    assert [r["mean_reverse_rank"] for r in ranks] == [2, 1, 1, None]
    assert [r["median_reverse_rank"] for r in ranks] == [1, 2, 2, None]
