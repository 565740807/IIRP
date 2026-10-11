"""Sector rotation: monthly and interval ranks across sector ETFs.

data/sector_rotation_sample.csv holds Yahoo daily bars (split-only adjusted,
as the long-term store keeps them) of seven sector ETFs and ^GSPC on the first
and the last October session of 2021–2025 only; the engine reports the
sessions in between as missing and still measures first open → last close.
The expected values below are computed by hand from those endpoints.
"""

import csv
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from iirp.analysis import rotation
from iirp.analysis.calendar import ET, last_completed_session, sessions
from iirp.analysis.sectors import chart_rows, industry_groups, request_prices, sector_etfs
from iirp.db import session
from iirp.market import stock_quotes, yahoo
from iirp.market.long_prices import history_start as long_history_start
from iirp.models import (
    Batch,
    Job,
    LongPriceBar,
    LongPriceSeries,
    Security,
    SourceObservation,
    now,
)
from sqlalchemy import func, select

from tests.jobs.test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401

SAMPLE = Path(__file__).with_name("data") / "sector_rotation_sample.csv"
ETFS = ["XLK", "XLV", "XLF", "XLY", "XLP", "XLE", "XLU"]
# The research cutoff: 2026 is the current year, 2021–2025 the five past years.
CUTOFF = date(2026, 1, 30)


def sample(symbol):
    with SAMPLE.open() as handle:
        return [{**row, "status": "VALID"} for row in csv.DictReader(handle) if row["symbol"] == symbol]


def rows_of(etfs):
    return [{"id": etf.lower(), "sector": etf.lower(), "etf": etf} for etf in etfs]


def ranked(etfs=ETFS, rank_by="absolute", years=5, bars=None, **params):
    rows = rows_of(etfs)
    bars = bars or {etf: sample(etf) for etf in etfs}
    computed = rotation.studies(rows, bars, sample("^GSPC"),
                                rotation.research_params(years=years, **params), CUTOFF)
    return rotation.rank(rows, computed, rank_by)


def october(periods):
    return next(period for period in periods if period["month"] == 10)


def row(period, etf):
    return next(item for item in period["rows"] if item["etf"] == etf)


def year(item, value):
    return next(entry for entry in item["years"] if entry["year"] == value)


def pct(value):
    return round(Decimal(value) * 100, 6)


# --- hand-checked October ------------------------------------------------------

def test_october_2025_xlk_and_xle_against_the_sp500_by_hand():
    period = october(ranked(["XLK", "XLE"]))
    xlk, xle = year(row(period, "XLK"), 2025), year(row(period, "XLE"), 2025)
    # XLK 150.339996337891 / 140.210006713867 − 1 = +7.224869%
    # XLE 44.064998626709 / 44.450000762939 − 1 = −0.866147%
    # ^GSPC 6840.200195312500 / 6664.919921875000 − 1 = +2.629893%
    assert pct(xlk["change"]) == Decimal("7.224869")
    assert pct(xle["change"]) == Decimal("-0.866147")
    # Excess over ^GSPC on the same first and last sessions.
    assert pct(xlk["excess"]) == Decimal("4.594976")
    assert pct(xle["excess"]) == Decimal("-3.496040")
    assert (xlk["rank"], xle["rank"]) == (1, 2)
    leaders = next(entry for entry in period["yearly"] if entry["year"] == 2025)
    assert (leaders["first"]["id"], leaders["last"]["id"], leaders["participants"]) == ("xlk", "xle", 2)
    assert leaders["first"]["change"] == xlk["change"]


def test_october_medians_ranks_and_top_three_years_by_hand():
    # October changes (%), largest first:
    # 2021  XLY 11.53  XLE 9.53  XLK 7.79 | XLF 6.99 | XLV 4.98  XLU 4.11  XLP 3.19
    # 2022  XLE 20.63  XLF 10.83  XLV 8.82 | XLP 8.23 | XLK 6.81  XLY 1.20  XLU 0.30
    # 2023  XLU 1.88  XLK 0.00  XLP −1.15 | XLF −2.12 | XLV −2.95  XLY −4.95  XLE −5.63
    # 2024  XLF 2.83  XLE 1.82  XLU −0.97 | XLK −1.27 | XLY −1.68  XLP −3.50  XLV −4.72
    # 2025  XLK 7.22  XLV 3.18  XLU 1.97 | XLY 0.29 | XLE −0.87  XLF −2.31  XLP −2.54
    period = october(ranked())
    medians = {item["etf"]: pct(item["stats"]["median"]) for item in period["rows"]}
    assert medians == {"XLK": Decimal("6.807552"), "XLV": Decimal("3.183116"), "XLF": Decimal("2.831856"),
                       "XLU": Decimal("1.877451"), "XLE": Decimal("1.815877"), "XLY": Decimal("0.292604"),
                       "XLP": Decimal("-1.150766")}
    assert {item["etf"]: item["rank"] for item in period["rows"]} == {
        "XLK": 1, "XLV": 2, "XLF": 3, "XLU": 4, "XLE": 5, "XLY": 6, "XLP": 7}
    tops = {item["etf"]: (item["top"]["count"], item["bottom"]["count"], item["top"]["n"])
            for item in period["rows"]}
    assert tops == {"XLK": (3, 1, 5), "XLE": (3, 2, 5), "XLU": (3, 2, 5), "XLF": (2, 1, 5),
                    "XLV": (2, 3, 5), "XLY": (1, 3, 5), "XLP": (1, 3, 5)}
    # XLK 2023: the close equals the open, so it is flat, not up: 3 of 5 up.
    xlk = row(period, "XLK")["stats"]
    assert (xlk["up"], xlk["flat"], xlk["n"], xlk["target_n"]) == (3, 1, 5, 5)
    assert Decimal(xlk["up_low"]) < Decimal("0.6") < Decimal(xlk["up_high"])
    assert (xlk["paired_n"], xlk["beat"]) == (5, 3)
    assert [(entry["year"], entry["first"]["id"], entry["last"]["id"]) for entry in period["yearly"][:5]] == [
        (2021, "xly", "xlp"), (2022, "xle", "xlu"), (2023, "xlu", "xle"), (2024, "xlf", "xlv"),
        (2025, "xlk", "xlp")]
    assert (period["yearly"][4]["start_date"], period["yearly"][4]["end_date"]) == ("2025-10-01", "2025-10-31")
    assert (period["best"], period["worst"]) == ("xlk", "xlp")
    # 2026 is the current year: listed, never counted.
    current = period["yearly"][-1]
    assert current["year"] == 2026 and current["current"] and current["participants"] == 0


def test_relative_ranks_use_the_median_excess_over_the_sp500():
    period = october(ranked(rank_by="relative"))
    # ^GSPC October: 2021 +6.676142, 2022 +7.263599, 2023 −2.117395, 2024 −0.907993,
    # 2025 +2.629893 (%). Each year's excess, then its median:
    # XLE +2.853113 +13.363662 −3.510160 +2.723870 −3.496040 → +2.723870 (beats 3 of 5)
    # XLK +1.111125 −0.456047 +2.117395 −0.358163 +4.594976 → +1.111125
    # XLF +0.312955 +3.564841 +0.000030 +3.739849 −4.942898 → +0.312955
    # XLU −2.568460 −6.963213 +3.994846 −0.058668 −0.661478 → −0.661478
    # XLV −1.694573 +1.556798 −0.835929 −3.808501 +0.553223 → −0.835929
    # XLY +4.850801 −6.062087 −2.831985 −0.769910 −2.337289 → −2.337289
    # XLP −3.490049 +0.964341 +0.966629 −2.596760 −5.172372 → −2.596760
    excess = {item["etf"]: pct(item["stats"]["median_excess"]) for item in period["rows"]}
    assert excess == {"XLE": Decimal("2.723870"), "XLK": Decimal("1.111125"), "XLF": Decimal("0.312955"),
                      "XLU": Decimal("-0.661478"), "XLV": Decimal("-0.835929"), "XLY": Decimal("-2.337289"),
                      "XLP": Decimal("-2.596760")}
    assert {item["etf"]: item["rank"] for item in period["rows"]} == {
        "XLE": 1, "XLK": 2, "XLF": 3, "XLU": 4, "XLV": 5, "XLY": 6, "XLP": 7}
    assert (row(period, "XLE")["stats"]["beat"], row(period, "XLE")["stats"]["paired_n"]) == (3, 5)
    # The absolute median puts XLK first; the excess puts XLE first.
    assert (period["best"], period["worst"]) == ("xle", "xlp")
    # A year's excess ranks match its change ranks: the same ^GSPC change is subtracted.
    absolute = october(ranked())
    for etf in ETFS:
        assert ([entry["rank"] for entry in row(period, etf)["years"]]
                == [entry["rank"] for entry in row(absolute, etf)["years"]])
        assert row(period, etf)["top"] == row(absolute, etf)["top"]


# --- short histories, missing years and the levels ----------------------------

def short(etf, first_year):
    """A copy of XLK's bars named ``etf`` that starts trading in ``first_year``."""
    return [{**bar, "symbol": etf} for bar in sample("XLK") if int(bar["date"][:4]) >= first_year]


def test_an_etf_with_fewer_years_counts_its_own_n_and_misses_earlier_ranks():
    etfs = ["XLK", "XLE", "XLC"]
    bars = {"XLK": sample("XLK"), "XLE": sample("XLE"), "XLC": short("XLC", 2023)}
    period = october(ranked(etfs, bars=bars))
    xlc = row(period, "XLC")
    assert (xlc["stats"]["n"], xlc["stats"]["target_n"]) == (3, 5)
    assert xlc["top"]["n"] == 3 and row(period, "XLK")["top"]["n"] == 5
    assert year(xlc, 2021)["rank"] is None and year(xlc, 2021)["status"] == "no_data"
    participants = {entry["year"]: entry["participants"] for entry in period["yearly"]}
    assert participants == {2021: 2, 2022: 2, 2023: 3, 2024: 3, 2025: 3, 2026: 0}
    # In 2021 only XLK and XLE are ranked; XLE (+9.53%) is above XLK (+7.79%).
    assert (year(row(period, "XLE"), 2021)["rank"], year(row(period, "XLK"), 2021)["rank"]) == (1, 2)


def test_an_etf_without_prices_is_listed_but_never_ranked():
    rows = rows_of(["XLK", "XLRE"])
    computed = rotation.studies(rows, {"XLK": sample("XLK"), "XLRE": []}, sample("^GSPC"),
                                rotation.research_params(years=5), CUTOFF)
    period = october(rotation.rank(rows, computed))
    xlre = row(period, "XLRE")
    assert (xlre["status"], xlre["rank"], xlre["stats"], xlre["years"]) == ("no_prices", None, None, [])
    assert row(period, "XLK")["rank"] == 1 and period["worst"] is None


def test_without_the_sp500_relative_ranks_are_empty():
    rows = rows_of(["XLK", "XLE"])
    bars = {etf: sample(etf) for etf in ("XLK", "XLE")}
    computed = rotation.studies(rows, bars, None, rotation.research_params(years=5), CUTOFF)
    period = october(rotation.rank(rows, computed, "relative"))
    assert all(item["rank"] is None and item["top"]["count"] == 0 for item in period["rows"])
    assert period["best"] is None
    assert october(rotation.rank(rows, computed, "absolute"))["best"] == "xlk"


def test_the_group_level_ranks_primary_etfs_and_leaves_pending_groups_out():
    groups = chart_rows("group")
    pending = {group["id"] for group in industry_groups() if group["coverage"] == "pending"}
    assert len(groups) == 18 and pending and not pending & {group["id"] for group in groups}
    assert "technology_hardware" in pending
    assert [row["etf"] for row in chart_rows("sector")] == sector_etfs()


# --- intervals --------------------------------------------------------------------

def daily(start, end, first, last):
    """Synthetic sessions from ``first`` (the first open) to ``last`` (the last close)."""
    days = sessions(start, end)
    return [{"date": day.isoformat(), "open": str(first if index == 0 else last),
             "high": str(max(first, last)), "low": str(min(first, last)),
             "close": str(last if index == len(days) - 1 else first), "status": "VALID"}
            for index, day in enumerate(days)]


def test_an_interval_across_the_year_end_ranks_each_year():
    # 12-15 → 01-15: the 2024 year runs from 2024-12-16 (12-15 is a Sunday) to 2025-01-15.
    up = daily(date(2024, 12, 16), date(2025, 1, 15), 100, 110)
    down = daily(date(2024, 12, 16), date(2025, 1, 15), 100, 95)
    market = daily(date(2024, 12, 16), date(2025, 1, 15), 100, 102)
    rows = rows_of(["AAA", "BBB"])
    params = rotation.research_params("interval", 1, "12-15", "01-15")
    computed = rotation.studies(rows, {"AAA": down, "BBB": up}, market, params, date(2025, 3, 3))
    (period,) = rotation.rank(rows, computed)
    assert period["cross_year"] and (period["start_mmdd"], period["end_mmdd"]) == ("12-15", "01-15")
    aaa, bbb = row(period, "AAA"), row(period, "BBB")
    assert year(bbb, 2024)["start_date"] == "2024-12-16" and year(bbb, 2024)["end_date"] == "2025-01-15"
    assert (Decimal(year(bbb, 2024)["change"]), Decimal(year(aaa, 2024)["change"])) == (Decimal("0.1"), Decimal("-0.05"))
    assert (bbb["rank"], aaa["rank"]) == (1, 2)
    assert Decimal(bbb["stats"]["median_excess"]) == Decimal("0.08")
    assert period["yearly"][0]["first"]["id"] == "bbb"
    with pytest.raises(ValueError):
        rotation.research_params("interval", 10, "03-01", "03-01")
    with pytest.raises(ValueError):
        rotation.research_params("monthly", 11)


# --- local reads only -----------------------------------------------------------

def store(symbol, bars, *, first=None):
    """One long-term series with these bars, as the daily store keeps them."""
    with session() as s, s.begin():
        security = Security(symbol=symbol, instrument="ETF", calendar="XNYS", status="VERIFIED")
        s.add(security)
        s.flush()
        days = [date.fromisoformat(bar["date"]) for bar in bars]
        series = LongPriceSeries(symbol=symbol, security_id=security.id, retain_from=first or min(days),
                                 start_date=first or min(days), end_date=max(days),
                                 complete_through=max(days), fetched_at=now(), needs_full=False, details={})
        s.add(series)
        s.flush()
        for bar in bars:
            s.add(LongPriceBar(cache_id=series.id, session_date=date.fromisoformat(bar["date"]),
                               open=bar["open"], high=bar["high"], low=bar["low"], close=bar["close"],
                               status="VALID"))


def app():
    from iirp.api.app import app as application
    return application


def source_requests():
    with session() as s:
        return s.scalar(select(func.count()).select_from(SourceObservation))


def test_reads_use_the_long_term_store_without_any_provider_request(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("sector rotation must not call a provider")

    monkeypatch.setattr(yahoo, "fetch_market", forbidden)
    monkeypatch.setattr(stock_quotes, "fetch_stock_quotes", forbidden)
    rotation.forget()
    for symbol in ("^GSPC", "XLK", "XLE"):
        store(symbol, sample(symbol))
    before = source_requests()
    with TestClient(app()) as client:
        responses = [client.get("/api/v1/sectors/rotation", params=params) for params in (
            {}, {"rank_by": "relative"}, {"level": "group"}, {"years": 5},
            {"kind": "interval", "start": "09-20", "end": "10-15"},
            {"kind": "interval", "start": "12-15", "end": "01-15", "level": "group", "rank_by": "relative"})]
        assert [response.status_code for response in responses] == [200] * 6
        body = responses[0].json()
        assert body["years"] == 10 and body["benchmark_status"] == "available"
        assert len(body["periods"]) == 12 and len(body["items"]) == 11
        # Each month has its ranks; the full rows are of this month only.
        assert body["detail"]["month"] == body["current_month"] and len(body["detail"]["rows"]) == 11
        assert all(len(period["cells"]) == 11 and "rows" not in period for period in body["periods"])
        chosen = client.get("/api/v1/sectors/rotation", params={"month": 10}).json()
        xlk = next(item for item in chosen["detail"]["rows"] if item["etf"] == "XLK")
        assert pct(year(xlk, 2025)["change"]) == Decimal("7.224869")
        cell = next(item for item in chosen["periods"][9]["cells"] if item["id"] == xlk["id"])
        assert (cell["rank"], cell["n"], xlk["stats"]["target_n"]) == (xlk["rank"], 5, 10)
        assert {item["etf"]: item["status"] for item in body["items"]}["XLV"] == "no_prices"
        assert len(responses[2].json()["items"]) == 18
        assert len(responses[4].json()["periods"]) == 1
        for params in ({"years": 11}, {"kind": "interval"}, {"kind": "interval", "start": "03-01", "end": "03-01"},
                       {"kind": "interval", "start": "13-01", "end": "03-01"}, {"level": "industry"},
                       {"month": 13}):
            assert client.get("/api/v1/sectors/rotation", params=params).status_code == 422
    assert source_requests() == before == 0
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 0
        assert s.scalar(select(func.count()).select_from(Batch)) == 0


def test_a_computation_is_kept_24_hours_and_redone_when_the_store_changes():
    rotation.forget()
    for symbol in ("^GSPC", "XLK"):
        store(symbol, sample(symbol))
    at = datetime(2026, 1, 30, 18, tzinfo=ET)
    with session() as s:
        first = rotation.read_rotation(s, years=5, at=at)
        again = rotation.read_rotation(s, years=5, rank_by="relative", at=at + timedelta(hours=1))
        assert again["computed_at"] == first["computed_at"]
        assert first["expires_at"] == (at + timedelta(hours=24)).isoformat()
        later = rotation.read_rotation(s, years=5, at=at + timedelta(hours=24, minutes=1))
        assert later["computed_at"] != first["computed_at"]
    with session() as s, s.begin():
        s.scalar(select(LongPriceSeries).where(LongPriceSeries.symbol == "XLK")).generation += 1
    with session() as s:
        changed = rotation.read_rotation(s, years=5, month=10, at=at + timedelta(hours=24, minutes=2))
        assert changed["computed_at"] != later["computed_at"]
        assert later["detail"]["month"] == 1 and changed["detail"]["month"] == 10
        xlk = row(changed["detail"], "XLK")
        assert pct(year(xlk, 2025)["change"]) == Decimal("7.224869")
        assert changed["historical_years"] == [2021, 2022, 2023, 2024, 2025]


def test_the_sector_page_needs_no_fetch_for_etfs_the_long_store_holds_in_full():
    at = now()
    completed = last_completed_session(at)
    kept_from = long_history_start(at)
    bars = daily(completed - timedelta(days=10), completed, 100, 101)
    for etf in sector_etfs():
        store(etf, bars, first=kept_from)
    with session() as s, s.begin():
        # Ten years back from today, not January 1 ten years ago: the store's own window.
        assert request_prices(s, at=at) == {"requested": [], "batch_ids": []}
