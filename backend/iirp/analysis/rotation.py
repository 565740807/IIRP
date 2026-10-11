"""Sector rotation: which sectors lead and lag in each month, or in one interval.

Every ETF's months come from the monthly and interval research engine
(``research.compute_research``) unchanged: one candle per year from the first
session's open to the last session's close, statistics over the complete past
years only (the current year is listed separately), the up count with its
Wilson 95% interval, and ^GSPC over the same first and last sessions for the
excess and the beat count. An ETF that started trading later (XLC from June
2018) is counted over the years it has; nothing is filled in.

This module ranks those results across the sectors (or the industry groups
that have a primary ETF):

- per period, by the median change ("absolute") or by the median excess over
  ^GSPC ("relative"); 1 is the largest, equal values share a rank;
- per year, among the ETFs with a value that year only, by the year's change
  or excess; an ETF is in the top three when fewer than three others are above
  it and in the bottom three when fewer than three others are below it;
- per year, the first and the last ETF.

Prices are read from the long-term daily store only; nothing here asks a
provider. A computation is kept for 24 hours like other analysis results and
is redone from the local store when it expires or when the store changes.
"""

import threading
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from iirp.analysis.calendar import ET, last_completed_session
from iirp.analysis.research import _interval_rules, _mmdd, compute_research, plan_scope
from iirp.analysis.sectors import chart_rows
from iirp.models import LongPriceBar, LongPriceSeries, now

LEVELS = ("sector", "group")
KINDS = ("monthly", "interval")
RANK_BY = ("absolute", "relative")
BENCHMARK = "^GSPC"
DEFAULT_YEARS = 10
MAX_YEARS = 10
TOP = 3
LIFETIME = timedelta(hours=24)

# Per-ETF engine fields carried into each ranked row.
STAT_FIELDS = ("n", "target_n", "median", "mean", "q25", "q75", "best", "worst", "best_year",
               "worst_year", "up", "flat", "up_ratio", "up_low", "up_high", "coin_flip",
               "paired_n", "beat", "beat_ratio", "median_excess", "mean_excess",
               "benchmark_median", "whisker_low", "whisker_high", "outliers",
               "benchmark_excess_box")


PLACES = Decimal("1e-10")


def _short(value: str | None) -> str | None:
    """A ratio for the response, to ten decimal places (ranks use the full value)."""
    return None if value is None else format(Decimal(value).quantize(PLACES), "f")


def _short_stats(stats: dict) -> dict:
    output = {}
    for key, value in stats.items():
        if isinstance(value, dict):
            output[key] = _short_stats(value)
        elif isinstance(value, list):
            output[key] = [_short(item) for item in value]
        elif isinstance(value, str):
            output[key] = _short(value)
        else:
            output[key] = value
    return output


def _value(candle: dict, rank_by: str) -> Decimal | None:
    if candle["status"] not in ("complete", "in_progress"):
        return None
    value = candle.get("change") if rank_by == "absolute" else candle.get("excess")
    return Decimal(value) if value is not None else None


def _ranks(values: dict[str, Decimal | None]) -> dict[str, tuple[int, int] | None]:
    """(rank from the top, rank from the bottom) among the keys with a value."""
    known = [value for value in values.values() if value is not None]
    return {key: (1 + sum(other > value for other in known), 1 + sum(other < value for other in known))
            if value is not None else None for key, value in values.items()}


def _entry(row: dict, candle: dict) -> dict:
    return {"id": row["id"], "change": _short(candle.get("change")), "excess": _short(candle.get("excess"))}


def _period(index: int, rows: list[dict], studies: dict[str, dict | None], rank_by: str) -> dict:
    """One month (or the interval) ranked across the ETFs."""
    present = [row for row in rows if studies.get(row["etf"]) is not None]
    periods = {row["etf"]: studies[row["etf"]]["periods"][index] for row in present}
    shape = next(iter(periods.values()), None)
    metric = "median" if rank_by == "absolute" else "median_excess"
    overall = _ranks({row["id"]: Decimal(stats[metric])
                      if (stats := periods[row["etf"]]["stats"])[metric] is not None and stats["n"] else None
                      for row in present})
    yearly, by_year = [], {}
    for position, shown in enumerate(shape["years"] if shape else []):
        candles = {row["id"]: periods[row["etf"]]["years"][position] for row in present}
        order = _ranks({key: _value(candle, rank_by) for key, candle in candles.items()})
        by_year[shown["year"]] = order
        # Config order breaks ties, so the first and the last are stable.
        ranked = sorted((ranks[0], -ranks[1], position, row) for position, row in enumerate(present)
                        if (ranks := order[row["id"]]) is not None)
        lead, tail = (ranked[0][3], ranked[-1][3]) if ranked else (None, None)
        dates = candles[lead["id"]] if lead else {}
        yearly.append({
            "year": shown["year"], "current": shown["current"],
            "start_date": dates.get("start_date"), "end_date": dates.get("end_date"),
            "in_progress": any(candle["status"] == "in_progress" for candle in candles.values()),
            "participants": len(ranked),
            "first": _entry(lead, candles[lead["id"]]) if lead else None,
            "last": _entry(tail, candles[tail["id"]]) if len(ranked) > 1 else None,
        })
    output_rows = []
    for row in rows:
        study = periods.get(row["etf"])
        if study is None:
            output_rows.append({**row, "status": "no_prices", "rank": None,
                                "top": {"count": 0, "n": 0}, "bottom": {"count": 0, "n": 0},
                                "stats": None, "years": []})
            continue
        stats = study["stats"]
        values, top, bottom, counted = [], 0, 0, 0
        for position, candle in enumerate(study["years"]):
            place = by_year[candle["year"]].get(row["id"])
            values.append({"year": candle["year"], "current": candle["current"], "status": candle["status"],
                           "start_date": candle.get("start_date"), "end_date": candle.get("end_date"),
                           "change": _short(candle.get("change")), "excess": _short(candle.get("excess")),
                           "rank": place[0] if place else None})
            if candle["current"] or candle["status"] != "complete" or place is None:
                continue
            counted += 1
            top += place[0] <= TOP
            bottom += place[1] <= TOP
        output_rows.append({
            **row, "status": "available", "rank": overall[row["id"]][0] if overall[row["id"]] else None,
            "top": {"count": top, "n": counted}, "bottom": {"count": bottom, "n": counted},
            "stats": _short_stats({key: stats.get(key) for key in STAT_FIELDS}), "years": values,
        })
    ranked_rows = sorted((item["rank"], position) for position, item in enumerate(output_rows)
                         if item["rank"] is not None)
    best = output_rows[ranked_rows[0][1]]["id"] if ranked_rows else None
    worst = output_rows[ranked_rows[-1][1]]["id"] if len(ranked_rows) > 1 else None
    if shape is None:
        return {"key": str(index), "month": None, "start_mmdd": "", "end_mmdd": "", "cross_year": False,
                "best": None, "worst": None, "rows": output_rows, "yearly": []}
    return {"key": shape["key"], "month": shape["month"], "start_mmdd": shape["start_mmdd"],
            "end_mmdd": shape["end_mmdd"], "cross_year": shape["cross_year"],
            "best": best, "worst": worst, "rows": output_rows, "yearly": yearly}


def studies(rows: list[dict], bars: dict[str, list[dict]], benchmark: list[dict] | None,
            params: dict, cutoff: date) -> dict[str, dict | None]:
    """The research result of each ETF with daily bars, paired with ^GSPC."""
    reference = ({"symbol": BENCHMARK, "status": "available", "bars": benchmark}
                 if benchmark else {"symbol": BENCHMARK, "status": "no_prices"})
    return {row["etf"]: compute_research(params, bars[row["etf"]], today=cutoff, benchmark=reference)
            if bars.get(row["etf"]) else None for row in rows}


def rank(rows: list[dict], computed: dict[str, dict | None], rank_by: str = "absolute") -> list[dict]:
    """Every period of the computed studies, ranked across the ETFs."""
    if rank_by not in RANK_BY:
        raise ValueError(rank_by)
    count = max((len(study["periods"]) for study in computed.values() if study), default=0)
    return [_period(index, rows, computed, rank_by) for index in range(count)]


def overview(period: dict) -> dict:
    """A period without its per-row detail: each row's rank and headline numbers."""
    cells = [{"id": row["id"], "rank": row["rank"], "n": (stats := row["stats"] or {}).get("n", 0),
              "up": stats.get("up", 0), "median": stats.get("median"),
              "median_excess": stats.get("median_excess")} for row in period["rows"]]
    return {**{key: value for key, value in period.items() if key != "rows"}, "cells": cells}


def research_params(kind: str = "monthly", years: int = DEFAULT_YEARS,
                    start_mmdd: str | None = None, end_mmdd: str | None = None) -> dict:
    """The research engine's request for these choices, checked."""
    if kind not in KINDS:
        raise ValueError(kind)
    if isinstance(years, bool) or not isinstance(years, int) or not 1 <= years <= MAX_YEARS:
        raise ValueError(f"years must be between 1 and {MAX_YEARS}")
    params = {"kind": kind, "historical_years": years}
    if kind == "interval":
        if start_mmdd is None or end_mmdd is None or _mmdd(start_mmdd) == _mmdd(end_mmdd):
            raise ValueError("An interval needs two different month-days")
        params.update(start_mmdd=start_mmdd, end_mmdd=end_mmdd)
    return params


# --- local reads -------------------------------------------------------------------

_memory: dict[tuple, tuple[datetime, dict]] = {}
_memory_lock = threading.Lock()
MEMORY_SIZE = 16


def _series(s, symbols: list[str]) -> dict[str, LongPriceSeries]:
    rows = s.scalars(select(LongPriceSeries).where(LongPriceSeries.symbol.in_(symbols),
                                                   LongPriceSeries.fetched_at.is_not(None)))
    return {row.symbol: row for row in rows}


def _bars(s, series: dict[str, LongPriceSeries], start: date, end: date) -> dict[str, list[dict]]:
    names = {row.id: symbol for symbol, row in series.items()}
    result: dict[str, list[dict]] = {symbol: [] for symbol in series}
    if not names:
        return result
    query = (select(LongPriceBar.cache_id, LongPriceBar.session_date, LongPriceBar.open, LongPriceBar.high,
                    LongPriceBar.low, LongPriceBar.close, LongPriceBar.status)
             .where(LongPriceBar.cache_id.in_(names), LongPriceBar.session_date.between(start, end))
             .order_by(LongPriceBar.cache_id, LongPriceBar.session_date))
    for row in s.execute(query):
        result[names[row.cache_id]].append({"date": row.session_date.isoformat(), "open": row.open,
                                            "high": row.high, "low": row.low, "close": row.close,
                                            "status": row.status})
    return result


def _first_dates(bars: dict[str, list[dict]]) -> dict[str, str | None]:
    return {symbol: next((bar["date"] for bar in rows if bar["status"] == "VALID"), None)
            for symbol, rows in bars.items()}


def _remember(key: tuple, at: datetime, value: dict) -> None:
    with _memory_lock:
        for old in [old for old, (made, _) in _memory.items() if made + LIFETIME <= at]:
            del _memory[old]
        while len(_memory) >= MEMORY_SIZE:
            del _memory[min(_memory, key=lambda old: _memory[old][0])]
        _memory[key] = (at, value)


def _recall(key: tuple, at: datetime) -> tuple[datetime, dict] | None:
    with _memory_lock:
        found = _memory.get(key)
    return found if found and found[0] + LIFETIME > at else None


def forget() -> None:
    with _memory_lock:
        _memory.clear()


def read_rotation(s, *, level: str = "sector", kind: str = "monthly", years: int = DEFAULT_YEARS,
                  rank_by: str = "absolute", start_mmdd: str | None = None, end_mmdd: str | None = None,
                  month: int | None = None, at: datetime | None = None) -> dict:
    """Ranked months (or one interval) of the sectors or industry groups, from local prices.

    Every period carries its ranks and leaders; the full rows (statistics, top
    and bottom three counts and each year) are given for one period only: the
    chosen month, this month by default, or the interval.
    """
    if level not in LEVELS:
        raise ValueError(level)
    if rank_by not in RANK_BY:
        raise ValueError(rank_by)
    if month is not None and not 1 <= month <= 12:
        raise ValueError(month)
    params = research_params(kind, years, start_mmdd, end_mmdd)
    at = at or now()
    cutoff = last_completed_session(at)
    rows = chart_rows(level)
    symbols = [row["etf"] for row in rows] + [BENCHMARK]
    series = _series(s, symbols)
    fingerprint = tuple(sorted((symbol, row.generation, str(row.complete_through), str(row.fetched_at))
                               for symbol, row in series.items()))
    key = (level, tuple(sorted(params.items())), cutoff, fingerprint)
    remembered = _recall(key, at)
    if remembered is None:
        start, end = plan_scope(params, cutoff)
        bars = _bars(s, series, start, end)
        computed = studies(rows, bars, bars.get(BENCHMARK), params, cutoff)
        remembered = (at, {"studies": computed, "first_dates": _first_dates(bars)})
        _remember(key, at, remembered[1])
    computed_at, saved = remembered
    computed = saved["studies"]
    example = next((study for study in computed.values() if study), None)
    if kind == "interval":
        _, _, cross, current = _interval_rules(params, cutoff)
    else:
        cross, current = False, cutoff.year
    items = []
    for row in rows:
        stored = series.get(row["etf"])
        items.append({**row, "status": "available" if computed.get(row["etf"]) else "no_prices",
                      "first_date": saved["first_dates"].get(row["etf"]),
                      "complete_through": str(stored.complete_through) if stored else None})
    benchmark = series.get(BENCHMARK)
    current_month = at.astimezone(ET).month
    periods = rank(rows, computed, rank_by)
    chosen = (month or current_month) if kind == "monthly" else None
    detail = next((period for period in periods if period["month"] == chosen), periods[0] if periods else None)
    return {
        "level": level, "kind": kind, "years": years, "rank_by": rank_by,
        "start_mmdd": params.get("start_mmdd"), "end_mmdd": params.get("end_mmdd"), "cross_year": cross,
        "historical_years": example["metadata"]["historical_years"] if example
        else list(range(current - years, current)),
        "current_year": current, "current_month": current_month,
        "cutoff_date": cutoff.isoformat(), "benchmark": BENCHMARK,
        "benchmark_status": "available" if saved["first_dates"].get(BENCHMARK) else "no_prices",
        "benchmark_complete_through": str(benchmark.complete_through) if benchmark else None,
        "computed_at": computed_at.isoformat(), "expires_at": (computed_at + LIFETIME).isoformat(),
        "items": items, "periods": [overview(period) for period in periods],
        "detail": detail,
    }
