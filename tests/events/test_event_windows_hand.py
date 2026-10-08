"""Earnings and event windows checked by hand against local daily bars (S6b).

``data/aapl_gspc_sample.csv`` holds AAPL and S&P 500 daily bars (split-only,
as cached from Yahoo) around two events:

1. AAPL FY2025 Q4 earnings, published 2025-10-30 after the close
   → R = Fri 2025-10-31, C(R-1) = close of 2025-10-30.
2. Apple WWDC 2025 keynote, Mon 2025-06-09 during the session
   → R = 2025-06-09, C(R-1) = close of Fri 2025-06-06.

The expected values below were worked out from the CSV with a calculator
(n = 5), independently of the engine.
"""

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from iirp.analysis.event_windows import analyze_events

DATA = Path(__file__).parent / "data" / "aapl_gspc_sample.csv"


def bars(symbol):
    with DATA.open() as source:
        return [{**row, "status": "VALID"} for row in csv.DictReader(source) if row["symbol"] == symbol]


def percent(value):
    return round(Decimal(value) * 100, 4)


EARNINGS = {"ticker": "AAPL", "date": "2025-10-30", "session": "after_close", "name": "FY2025 Q4 earnings",
            "fiscal_year": 2025, "fiscal_quarter": 4}
WWDC = {"ticker": "AAPL", "date": "2025-06-09", "session": "during", "name": "WWDC 2025 keynote"}


@pytest.fixture(scope="module")
def results():
    benchmark = {"symbol": "^GSPC", "status": "available", "bars": bars("^GSPC")}
    earnings = analyze_events([EARNINGS], bars("AAPL"), n=5, cutoff=date(2025, 11, 10), kind="earnings",
                              benchmark=benchmark)
    event = analyze_events([WWDC], bars("AAPL"), n=5, cutoff=date(2025, 11, 10), benchmark=benchmark)
    return earnings["rows"][0], event["rows"][0], earnings


def test_after_close_earnings_by_hand(results):
    row = results[0]
    # R is the session after the publication date; before starts at C(R-1-5) = 10-23.
    assert (row["reaction_date"], row["baseline_date"]) == ("2025-10-31", "2025-10-30")
    w = row["windows"]
    assert (w["before"]["start_date"], w["after"]["end_date"]) == ("2025-10-23", "2025-11-07")
    # before   = 271.399993896484 / 259.579986572266 − 1
    # reaction = 270.369995117188 / 271.399993896484 − 1
    # gap      = 276.989990234375 / 271.399993896484 − 1   (open of R)
    # after    = 268.470001220703 / 270.369995117188 − 1
    assert percent(w["before"]["value"]) == Decimal("4.5535")
    assert percent(w["reaction"]["value"]) == Decimal("-0.3795")
    assert percent(w["gap"]["value"]) == Decimal("2.0597")
    assert percent(w["after"]["value"]) == Decimal("-0.7027")
    # The S&P 500 over the same dates: 6738.44 → 6822.34 → 6840.20 (open 6879.17) → 6728.80.
    assert percent(w["before"]["benchmark"]) == Decimal("1.2451")
    assert percent(w["reaction"]["benchmark"]) == Decimal("0.2618")
    assert percent(w["gap"]["benchmark"]) == Decimal("0.8330")
    assert percent(w["after"]["benchmark"]) == Decimal("-1.6286")
    assert percent(w["reaction"]["excess"]) == Decimal("-0.6413")  # −0.3795 − 0.2618
    # The reaction-day candle from C(R-1): open = gap, close = reaction, high 277.32, low 269.16.
    candle = row["reaction_candle"]
    assert candle["open"] == w["gap"]["value"] and candle["close"] == w["reaction"]["value"]
    assert (percent(candle["high"]), percent(candle["low"])) == (Decimal("2.1813"), Decimal("-0.8253"))
    # The path is C(t)/C(R-1) − 1 from R-5 to R+5: 0 at R-1, the reaction at R.
    path = {point["offset"]: point for point in row["path"]}
    assert [point["date"] for point in row["path"]][:2] == ["2025-10-24", "2025-10-27"]
    assert Decimal(path[-1]["value"]) == 0 and path[0]["value"] == w["reaction"]["value"]
    assert row["path"][-1]["date"] == "2025-11-07"
    assert (1 + Decimal(path[5]["value"])) == pytest.approx(
        (1 + Decimal(w["reaction"]["value"])) * (1 + Decimal(w["after"]["value"])))


def test_intraday_event_by_hand(results):
    row = results[1]
    # During the session: R is the event day itself; C(R-1) is Friday's close.
    assert (row["reaction_date"], row["baseline_date"]) == ("2025-06-09", "2025-06-06")
    w = row["windows"]
    assert (w["before"]["start_date"], w["after"]["end_date"]) == ("2025-05-30", "2025-06-16")
    # before   = 203.919998168945 / 200.850006103516 − 1
    # reaction = 201.449996948242 / 203.919998168945 − 1
    # gap      = 204.389999389648 / 203.919998168945 − 1
    # after    = 198.419998168945 / 201.449996948242 − 1
    assert percent(w["before"]["value"]) == Decimal("1.5285")
    assert percent(w["reaction"]["value"]) == Decimal("-1.2113")
    assert percent(w["gap"]["value"]) == Decimal("0.2305")
    assert percent(w["after"]["value"]) == Decimal("-1.5041")
    assert row["notes"] == []


def test_statistics_carry_intervals_absolute_moves_and_benchmark(results):
    summary = results[2]["summary"]["reaction"]
    assert (summary["n"], summary["up"], summary["paired_n"], summary["beat"]) == (1, 0, 1, 0)
    # One event: |−0.3795%| is the median absolute move; Wilson interval of 0/1 is [0, 0.7935].
    assert percent(summary["abs_median"]) == Decimal("0.3795")
    assert Decimal(summary["up_low"]) == 0 and round(Decimal(summary["up_high"]), 4) == Decimal("0.7935")
    assert summary["coin_flip"] is True
    assert results[2]["benchmark"] == {"symbol": "^GSPC", "status": "available"}
    assert [q["fiscal_quarter"] for q in results[2]["quarters"]] == [4]
    path = {point["offset"]: point for point in results[2]["path"]}
    assert path[0]["n"] == 1 and path[0]["median"] == summary["median"]
