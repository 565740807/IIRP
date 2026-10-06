"""Regression: monthly return paths include the preceding month's close."""

from datetime import date
from decimal import Decimal

from iirp.analysis.calendar import sessions
from iirp.analysis.research import compute_research


def prices(missing=None):
    return [
        {"date": str(day), "close": "100" if day == date(2022, 12, 30) else "90", "status": "VALID"}
        for day in sessions(date(2022, 12, 30), date(2023, 1, 31))
        if day != missing
    ]


def result(kind="monthly", bars=None):
    return compute_research(
        {
            "kind": kind,
            "month": 1,
            "years": [2023],
            "current_year": 2024,
            "start_mmdd": "01-01",
            "end_mmdd": "01-31",
            "alignment": "trading",
            "comparison": "complete",
        },
        prices() if bars is None else bars,
        today=date(2024, 2, 1),
    )


def historical(computed):
    return next(row for row in computed["rows"] if row["year"] == 2023)


def test_monthly_first_session_drop_counts_from_shared_return_baseline():
    computed = result()
    row = historical(computed)
    assert row["baseline_date"] == "2022-12-30"
    assert Decimal(row["endpoint"]) == Decimal("-0.1")
    assert Decimal(row["max_drawdown"]) == Decimal("0.1")
    assert Decimal(computed["summary"]["worst_drawdown"]) == Decimal("0.1")
    january = next(
        cell for cell in computed["cells"] if cell["year"] == 2023 and cell["month"] == 1
    )
    assert Decimal(january["max_drawdown"]) == Decimal("0.1")


def test_interval_still_starts_from_its_own_first_close():
    row = historical(result(kind="interval"))
    assert row["baseline_date"] == "2023-01-03"
    assert Decimal(row["endpoint"]) == 0
    assert Decimal(row["max_drawdown"]) == 0


def test_monthly_missing_baseline_or_internal_close_prevents_full_drawdown():
    for missing in (date(2022, 12, 30), date(2023, 1, 10)):
        row = historical(result(bars=prices(missing)))
        assert row["max_drawdown"] is None
        assert not row["complete"]
