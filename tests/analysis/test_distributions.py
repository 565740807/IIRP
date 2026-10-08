"""Hand-computed distribution statistics, no provider calls."""

from decimal import Decimal

import pytest
from iirp.analysis.distributions import statistics, wilson_interval


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


def test_wilson_interval_matches_the_nist_formula():
    low, high = wilson_interval(6, 8)
    # p = 0.75, n = 8, z = 1.96: 40.9%–92.9%
    assert (round(low, 3), round(high, 3)) == (Decimal("0.409"), Decimal("0.929"))
    assert wilson_interval(0, 5)[0] == 0 and wilson_interval(5, 5)[1] == 1
    with pytest.raises(ValueError):
        wilson_interval(0, 0)
