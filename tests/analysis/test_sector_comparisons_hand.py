"""Independent checks of October 2025 NVDA and after-close AAPL sector excess.

CSV bars are local Yahoo samples, split-only adjusted. Expected ratios below
are computed from the stated endpoints, independently of the analysis engine.
"""

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

from iirp.analysis.event_windows import analyze_events
from iirp.analysis.research import compute_research

DATA = Path(__file__).with_name("data")


def bars(filename, symbol):
    with (DATA / filename).open() as source:
        return [{**row, "status": "VALID"} for row in csv.DictReader(source) if row["symbol"] == symbol]


def pct(value):
    return round(Decimal(value) * 100, 6)


def test_nvda_october_open_to_close_against_market_and_sector():
    filename = "nvda_gspc_xlk_sample.csv"
    market = {"symbol": "^GSPC", "status": "available", "bars": bars(filename, "^GSPC")}
    sector = {"symbol": "XLK", "status": "available", "source": "sp500_gics", "bars": bars(filename, "XLK")}
    result = compute_research({"kind": "monthly", "current_year": 2026, "historical_years": 1},
                              bars(filename, "NVDA"), today=date(2026, 1, 30), benchmark=market, sector=sector)
    october = next(period for period in result["periods"] if period["key"] == "10")
    row = next(row for row in october["years"] if row["year"] == 2025)
    # NVDA 202.490005493164 / 185.240005493164 - 1;
    # SPX 6840.200195312500 / 6664.919921875000 - 1;
    # XLK 150.339996337891 / 140.210006713867 - 1.
    assert pct(row["change"]) == Decimal("9.312243")
    assert pct(row["benchmark_change"]) == Decimal("2.629893")
    assert pct(row["sector_change"]) == Decimal("7.224869")
    assert pct(row["excess"]) == Decimal("6.682350")
    assert pct(row["sector_excess"]) == Decimal("2.087374")
    assert (october["stats"]["sector_beat"], october["stats"]["sector_paired_n"]) == (1, 1)
    assert pct(october["stats"]["sector_excess_box"]["median"]) == Decimal("2.087374")
    assert result["metadata"]["sector"]["source"] == "sp500_gics"
    assert result["metadata"]["sector_comparison_included"] is True
    interval = compute_research({"kind": "interval", "current_year": 2026, "historical_years": 1,
                                 "start_mmdd": "10-01", "end_mmdd": "10-31"},
                                bars(filename, "NVDA"), today=date(2026, 1, 30), benchmark=market, sector=sector)
    assert interval["periods"][0]["years"][0]["sector_excess"] == row["sector_excess"]


def test_aapl_after_close_reaction_against_xlk():
    from tests.events.test_event_windows_hand import DATA as AAPL_DATA
    with AAPL_DATA.open() as source:
        prices = [{**row, "status": "VALID"} for row in csv.DictReader(source) if row["symbol"] == "AAPL"]
    sector = {"symbol": "XLK", "status": "available", "source": "sp500_gics",
              "bars": bars("sector_etf_sample.csv", "XLK")}
    event = {"ticker": "AAPL", "date": "2025-10-30", "session": "after_close", "name": "Q4",
             "fiscal_year": 2025, "fiscal_quarter": 4}
    result = analyze_events([event], prices, n=5, cutoff=date(2025, 11, 10), kind="earnings", sector=sector)
    row = result["rows"][0]
    assert row["reaction_date"] == "2025-10-31"
    # AAPL 270.369995117188 / 271.399993896484 - 1;
    # XLK 150.339996337891 / 150.195007324219 - 1.
    reaction = row["windows"]["reaction"]
    assert pct(reaction["value"]) == Decimal("-0.379513")
    assert pct(reaction["sector"]) == Decimal("0.096534")
    assert pct(reaction["sector_excess"]) == Decimal("-0.476047")
    summary = result["summary"]["reaction"]
    assert (summary["sector_paired_n"], summary["sector_beat"]) == (1, 0)
    assert pct(summary["sector_median_excess"]) == Decimal("-0.476047")
    assert Decimal(result["path"][5]["sector_median"]) == Decimal(reaction["sector"])
