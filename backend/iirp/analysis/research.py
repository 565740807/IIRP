"""Monthly and interval research: first session open → last session close (D6).

Each year contributes one candle per period: the open of its first session,
the close of its last session, and the high and low in between. The change is
close / open − 1. Prices are the 24-hour cache's split-only adjusted bars; the
provider applies one split factor to open, high, low and close alike, so open
and close are on the same basis. The gap from the previous close to the first
open is not included, so monthly changes do not compound to a yearly change.

Ratios are decimal strings (not percent points). Statistics use the complete
past years only (D7: the current year is shown separately). A benchmark change
uses the stock's own first and last session dates. This module neither fetches
nor adjusts prices; graphs, tables, the conclusion and the CSV export all read
the result it returns, and the frontend does not recompute it.
"""

import calendar as gregorian
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from iirp.analysis.calendar import ET, calendar_version, completed_through, sessions
from iirp.analysis.distributions import (
    DistributionStatistics,
    paired_ratio,
    statistics,
    wilson_interval,
)
from iirp.analysis.prices import endpoint_change

CALCULATION_VERSION = "research-v11-open-close"
DEFAULT_YEARS = 8
MAX_YEARS = 30


class Candle(BaseModel):
    """One year of one period."""

    year: int
    current: bool
    status: Literal["complete", "in_progress", "not_started", "no_data", "incomplete"]
    period_start: str
    period_end: str
    start_date: str | None = None  # first session (actual)
    end_date: str | None = None  # last session, or the last completed one while in progress
    sessions: int = 0
    expected_sessions: int = 0
    missing_dates: list[str] = Field(default_factory=list)
    open: str | None = None
    high: str | None = None
    low: str | None = None
    close: str | None = None
    change: str | None = None
    # High and low relative to the open, for candles drawn from a common zero.
    high_change: str | None = None
    low_change: str | None = None
    benchmark_open: str | None = None
    benchmark_close: str | None = None
    benchmark_change: str | None = None
    excess: str | None = None


class PeriodStats(DistributionStatistics):
    """Complete past years only; ``target_n`` is how many were asked for."""

    target_n: int
    n: int
    median: str | None = None
    mean: str | None = None
    q25: str | None = None
    q75: str | None = None
    best: str | None = None
    worst: str | None = None
    best_year: int | None = None
    worst_year: int | None = None
    up: int = 0
    flat: int = 0
    # Wilson score 95% interval of the share of up years.
    up_low: str | None = None
    up_high: str | None = None
    # The interval contains one half: no different from a coin flip.
    coin_flip: bool | None = None
    paired_n: int = 0
    beat: int = 0
    median_excess: str | None = None
    mean_excess: str | None = None
    benchmark_median: str | None = None


class Period(BaseModel):
    key: str
    month: int | None = None
    start_mmdd: str
    end_mmdd: str
    cross_year: bool = False
    stats: PeriodStats
    years: list[Candle]


class BenchmarkInfo(BaseModel):
    symbol: str
    status: str


class ResearchMetadata(BaseModel):
    # Publication adds source, cache and dependency facts.
    model_config = ConfigDict(extra="allow")
    params: dict[str, Any]
    historical_years: list[int]
    current_year: int
    cutoff_date: str
    calendar: str
    calendar_version: str
    calculation_version: str = CALCULATION_VERSION
    price_basis: str = "split_only"
    benchmark: BenchmarkInfo | None = None


class ResearchResult(BaseModel):
    kind: Literal["monthly", "interval"]
    metadata: ResearchMetadata
    periods: list[Period]


# --- shared bar helpers (also used by the Insider before/after windows) --------


def _text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _ratio(first: Decimal | None, last: Decimal | None) -> str | None:
    return _text(endpoint_change(first, last).value)


def _positive(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number > 0 else None
    except InvalidOperation:
        return None


def _bars_by_date(bars: list[dict]) -> dict[date, dict[str, Decimal | None]]:
    result = {}
    for bar in bars:
        day = date.fromisoformat(str(bar["date"])[:10])
        if day in result:
            # Selecting a duplicate is version reconciliation, never arithmetic.
            raise ValueError(f"Duplicate daily bar for {day}; select one published version first")
        valid = bar.get("status") == "VALID"
        result[day] = {
            key: _positive(bar.get(key)) if valid else None
            for key in ("open", "high", "low", "close")
        }
    return result


def _price(index: dict, day: date, cutoff: date, field: str = "close") -> Decimal | None:
    if day > cutoff:
        return None
    return index.get(day, {}).get(field)


def _point(day: date, x: int, baseline: Decimal | None, index: dict, cutoff: date) -> dict:
    value = _price(index, day, cutoff)
    status = (
        "not_yet_formed"
        if day > cutoff
        else "missing_baseline"
        if baseline is None
        else "missing_price"
        if value is None
        else "available"
    )
    return {"x": x, "date": day.isoformat(), "value": _ratio(baseline, value), "status": status}


# --- conditions ------------------------------------------------------------------


def _mmdd(value: str) -> tuple[int, int]:
    try:
        month, day = (int(piece) for piece in value.split("-"))
        date(2000, month, day)
    except (ValueError, AttributeError):
        raise ValueError(f"Invalid month-day: {value}") from None
    return month, day


def _mapped_day(year: int, month_day: tuple[int, int]) -> date:
    month, day = month_day
    if month == 2 and day == 29 and not gregorian.isleap(year):
        day = 28
    return date(year, month, day)


def _interval_rules(params: dict, today: date) -> tuple[tuple, tuple, bool, int]:
    """(start, end, crosses the year end, current year). An end before the start
    crosses into the next year; the current year is the latest one already begun
    or, before this year's start, this calendar year."""
    start = _mmdd(params.get("start_mmdd", "09-20"))
    end = _mmdd(params.get("end_mmdd", "10-15"))
    cross = end < start
    current = today.year - int(cross and (today.month, today.day) <= end)
    return start, end, cross, int(params.get("current_year") or current)


def _selected_years(params: dict, current: int) -> list[int]:
    count = params.get("historical_years", DEFAULT_YEARS)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_YEARS:
        raise ValueError(f"historical_years must be between 1 and {MAX_YEARS}")
    return list(range(current - count, current))


def plan_scope(params: dict, today: date) -> tuple[date, date]:
    """Inclusive price bounds of every year in the research, never in the future."""
    params = {key: value for key, value in params.items() if value is not None}
    kind = params.get("kind", "monthly")
    if kind == "monthly":
        current = int(params.get("current_year") or today.year)
        years = [*_selected_years(params, current), current]
        start, end = date(min(years), 1, 1), min(today, date(current, 12, 31))
    elif kind == "interval":
        start_md, end_md, cross, current = _interval_rules(params, today)
        years = [*_selected_years(params, current), current]
        start = _mapped_day(min(years), start_md)
        end = min(today, _mapped_day(current + int(cross), end_md))
    else:
        raise ValueError(f"Unsupported research kind: {kind}")
    if start > end:
        raise ValueError("The requested range has not started; no price collection is due")
    return start, end


def period_ranges(params: dict, today: date) -> list[tuple[date, date]]:
    """Calendar bounds of each year's period(s), for reading only the bars needed."""
    params = {key: value for key, value in params.items() if value is not None}
    if params.get("kind", "monthly") == "monthly":
        current = int(params.get("current_year") or today.year)
        return [(date(year, 1, 1), date(year, 12, 31))
                for year in [*_selected_years(params, current), current]]
    start_md, end_md, cross, current = _interval_rules(params, today)
    return [(_mapped_day(year, start_md), _mapped_day(year + int(cross), end_md))
            for year in [*_selected_years(params, current), current]]


# --- computation -----------------------------------------------------------------


def _candle(year, current, first, last, index, cutoff, calendar, benchmark) -> dict:
    expected = sessions(first, last, calendar)
    done = [day for day in expected if day <= cutoff]
    row = {"year": year, "current": current, "period_start": first.isoformat(),
           "period_end": last.isoformat(), "expected_sessions": len(expected)}
    if not expected:
        return {**row, "status": "no_data"}
    if not done:
        return {**row, "status": "not_started"}
    in_progress = done[-1] < expected[-1]
    end = done[-1]
    if in_progress and index.get(end, {}).get("close") is None:
        # A period still running ends at its latest close: today's bar can lack
        # one until the provider settles it. A finished period never moves.
        end = max((day for day in done if index.get(day, {}).get("close") is not None), default=end)
    done = [day for day in done if day <= end]
    present = [day for day in done if index.get(day, {}).get("close") is not None
               and index[day].get("open") is not None]
    if not present:
        return {**row, "status": "no_data", "start_date": done[0].isoformat()}
    opening, closing = index.get(done[0], {}).get("open"), index.get(end, {}).get("close")
    high = max((index[day]["high"] for day in present if index[day].get("high") is not None), default=None)
    low = min((index[day]["low"] for day in present if index[day].get("low") is not None), default=None)
    row.update(start_date=done[0].isoformat(), end_date=end.isoformat(), sessions=len(present),
               missing_dates=[day.isoformat() for day in done if day not in present],
               open=_text(opening), close=_text(closing), high=_text(high), low=_text(low))
    if opening is None or closing is None:
        # Without the first open or the last close there is no change to report.
        return {**row, "status": "incomplete"}
    row.update(status="in_progress" if in_progress else "complete", change=_ratio(opening, closing),
               high_change=_ratio(opening, high), low_change=_ratio(opening, low))
    if benchmark is not None:
        other_open = benchmark.get(done[0], {}).get("open")
        other_close = benchmark.get(end, {}).get("close")
        row.update(benchmark_open=_text(other_open), benchmark_close=_text(other_close),
                   benchmark_change=_ratio(other_open, other_close))
        if row["benchmark_change"] is not None:
            with localcontext() as context:
                context.prec = 34
                row["excess"] = str(Decimal(row["change"]) - Decimal(row["benchmark_change"]))
    return row


def period_stats(candles: list[dict], target_n: int) -> dict:
    """Statistics of the complete past years of one period."""
    sample = [c for c in candles if not c["current"] and c["status"] == "complete"]
    values = statistics([c["change"] for c in sample])
    paired = [c for c in sample if c.get("excess") is not None]
    excess = statistics([c["excess"] for c in paired])
    output = {
        "target_n": target_n, "n": values["n"], "up": values["up"], "flat": values["flat"],
        **{key: values.get(key) for key in ("median", "mean", "q25", "q75", "up_ratio", "whisker_low", "whisker_high", "outliers")},
        "best": values.get("max"), "worst": values.get("min"),
        "paired_n": excess["n"], "beat": excess["up"],
        "median_excess": excess.get("median"), "mean_excess": excess.get("mean"),
        "beat_ratio": paired_ratio(excess["up"], excess["n"]),
        "benchmark_median": statistics([c["benchmark_change"] for c in paired]).get("median"),
    }
    if sample:
        ranked = sorted(sample, key=lambda c: Decimal(c["change"]))
        output["worst_year"], output["best_year"] = ranked[0]["year"], ranked[-1]["year"]
        low, high = wilson_interval(values["up"], values["n"])
        output.update(up_low=str(low), up_high=str(high), coin_flip=low <= Decimal("0.5") <= high)
    return PeriodStats.model_validate(output).model_dump()


def compute_research(
    params: dict, bars: list[dict], today: date | None = None, benchmark: dict | None = None,
) -> dict:
    """One security; ``today`` is the research cutoff (the last completed session)."""
    params = {key: value for key, value in params.items() if value is not None}
    kind = params.get("kind", "monthly")
    calendar = params.get("calendar", "XNYS")
    as_of = today or datetime.now(ET).date()
    cutoff = completed_through(today, calendar)
    index = _bars_by_date(bars)
    paired = None
    if benchmark is not None and benchmark.get("status") == "available":
        paired = _bars_by_date(benchmark.get("bars", []))
    if kind == "monthly":
        current = int(params.get("current_year") or as_of.year)
        bounds = [(month, f"{month:02d}-01", f"{month:02d}-{gregorian.monthrange(2001, month)[1]:02d}",
                   lambda year, m=month: (date(year, m, 1), date(year, m, gregorian.monthrange(year, m)[1])),
                   False)
                  for month in range(1, 13)]
    elif kind == "interval":
        start_md, end_md, cross, current = _interval_rules(params, as_of)
        bounds = [(None, f"{start_md[0]:02d}-{start_md[1]:02d}", f"{end_md[0]:02d}-{end_md[1]:02d}",
                   lambda year: (_mapped_day(year, start_md), _mapped_day(year + int(cross), end_md)),
                   cross)]
    else:
        raise ValueError(f"Unsupported research kind: {kind}")
    years = _selected_years(params, current)
    periods = []
    for month, start_mmdd, end_mmdd, span, cross in bounds:
        candles = [_candle(year, year == current, *span(year), index, cutoff, calendar, paired)
                   for year in [*years, current]]
        periods.append({
            "key": str(month) if month else "interval", "month": month,
            "start_mmdd": start_mmdd, "end_mmdd": end_mmdd, "cross_year": cross,
            "stats": period_stats(candles, len(years)), "years": candles,
        })
    result = {
        "kind": kind,
        "metadata": {
            "params": params, "historical_years": years, "current_year": current,
            "cutoff_date": cutoff.isoformat(), "calendar": calendar,
            "calendar_version": calendar_version(),
            "benchmark": {"symbol": benchmark["symbol"], "status": benchmark.get("status", "unknown")}
            if benchmark else None,
        },
        "periods": periods,
    }
    return ResearchResult.model_validate(result).model_dump(mode="json")
