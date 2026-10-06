"""Calendar-aware research from one already-qualified price version.

This module neither fetches nor adjusts prices. Ratios are decimal strings (not
percent points). Only bars marked VALID enter calculations, and every expected
session remains represented even when its price is missing. The returned object
is shared by graphs, tables and exports; a frontend must not recompute metrics.
"""

import calendar as gregorian
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, Field

from iirp.analysis.calendar import (
    ET,
    as_et,
    calendar_version,
    completed_through,
    next_regular_open_after,
    next_session,
    previous_session,
    reaction_session,
    session_window,
    sessions,
)
from iirp.analysis.distributions import (
    Distribution,
    MonthlyRanking,
    ResearchMethodology,
    add_distributions,
    statistics,
)
from iirp.analysis.prices import endpoint_change, maximum_drawdown

CALCULATION_VERSION = "research-v10-time-source-attribution"
WINDOWS = (1, 5, 20, 60)


class ResearchPoint(BaseModel):
    x: int
    date: str | None = None
    value: str | None = None
    status: str
    session: bool = True
    benchmark: str | None = None
    difference: str | None = None
    benchmark_status: str | None = None


class ResearchSeries(BaseModel):
    key: str
    label: str
    year: int | None = None
    group: Literal["historical", "current", "observation"]
    points: list[ResearchPoint]


class ResearchMetadata(BaseModel):
    source: str | None = None
    dataset_id: str | None = None
    data_version: str | None = None
    params: dict[str, Any]
    historical_years: list[int]
    current_year: int | None
    target_n: int
    cutoff_date: str
    calendar: str
    calendar_version: str
    calculation_version: str = CALCULATION_VERSION
    price_basis: str = "split_only"
    alignment: str
    comparison: str
    warnings: list[str] = Field(default_factory=list)
    methodology: ResearchMethodology | None = None


class ResearchResult(BaseModel):
    kind: Literal["monthly", "interval"]
    metadata: ResearchMetadata
    summary: dict[str, Any]
    cells: list[dict[str, Any]]
    series: list[ResearchSeries]
    rows: list[dict[str, Any]]
    effective_n: int
    exclusions: list[dict[str, Any]]
    distributions: list[Distribution] = Field(default_factory=list)
    benchmark: dict[str, Any] | None = None
    monthly_rankings: list[MonthlyRanking] = Field(default_factory=list)


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


def _year(value: Any) -> int:
    if isinstance(value, bool) or not str(value).isdigit() or not 2 <= int(value) <= 9998:
        raise ValueError("Year must be an integer between 2 and 9998")
    return int(value)


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not str(value).isdigit() or int(value) < 1:
        raise ValueError(f"{label} must be a positive integer")
    return int(value)


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
    start = _mmdd(params.get("start_mmdd", "03-15"))
    end = _mmdd(params.get("end_mmdd", "04-30"))
    cross = bool(params.get("cross_year", end < start))
    if not cross and end < start:
        raise ValueError("End precedes start; select cross_year")
    default_year = today.year - int(cross and (today.month, today.day) <= end)
    current = _year(params.get("anchor_start_year", params.get("current_year", default_year)))
    return start, end, cross, current


def _selected_years(params: dict, current: int | None) -> tuple[list[int], list[dict]]:
    count = _positive_int(params.get("historical_years", 8), "historical_years")
    excluded = {_year(value) for value in params.get("excluded_years", [])}
    if "years" in params and params["years"] is not None:
        candidates = sorted({_year(value) for value in params["years"]})
    elif current is not None:
        if current - count < 2:
            raise ValueError("Requested history extends outside the supported date domain")
        candidates = list(range(current - count, current))
    else:
        candidates = []
    output, reasons = [], []
    for year in candidates:
        reason = (
            "user_excluded"
            if year in excluded
            else "not_historical_year"
            if current is not None and year >= current
            else None
        )
        if reason:
            reasons.append({"year": year, "reason": reason})
        else:
            output.append(year)
    return output, reasons


def plan_scope(params: dict, today: date) -> tuple[date, date]:
    """Inclusive collection bounds, including needed baseline, never future."""
    params = {key: value for key, value in params.items() if value is not None}
    kind = params.get("kind", "monthly")
    name = params.get("calendar", "XNYS")
    if kind == "monthly":
        current = _year(params.get("current_year", today.year))
        years, _ = _selected_years(params, current)
        all_years = [*years, current]
        start = previous_session(date(min(all_years), 1, 1), name)
        end = min(today, date(max(all_years), 12, 31))
    elif kind == "interval":
        start_md, end_md, cross, current = _interval_rules(params, today)
        years, _ = _selected_years(params, current)
        all_years = [*years, current]
        start = min(_mapped_day(year, start_md) for year in all_years)
        end = min(today, max(_mapped_day(year + int(cross), end_md) for year in all_years))
    else:
        raise ValueError(f"Unsupported research kind: {kind}")
    if start > end:
        raise ValueError("The requested range has not started; no price collection is due")
    return start, end


def _statistics(values: list[str | None]) -> dict:
    return statistics(values)


def _path_statistics(series: list[dict]) -> list[dict]:
    grouped = defaultdict(list)
    for item in series:
        if item["group"] == "historical":
            for point in item["points"]:
                grouped[point["x"]].append(point["value"])
    return [{"x": x, **_statistics(grouped[x])} for x in sorted(grouped)]


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


def _calendar_points(
    start: date,
    end: date,
    expected: list[date],
    baseline_date: date | None,
    baseline: Decimal | None,
    index: dict,
    cutoff: date,
    *,
    monthly: bool,
) -> list[dict]:
    output = []
    expected_set = set(expected)
    prior = baseline_date if monthly else None
    cursor = start
    while cursor <= end:
        trading = cursor in expected_set
        if trading:
            prior = cursor
        # A 366-position reference year aligns month-days through February;
        # elapsed calendar days would shift March by one in leap samples.
        x = (
            cursor.day
            if monthly
            else (
                (cursor.year - start.year) * 366
                + date(2000, cursor.month, cursor.day).timetuple().tm_yday
                - date(2000, start.month, start.day).timetuple().tm_yday
            )
        )
        if prior is None:
            point = {"x": x, "date": None, "value": None, "status": "no_session_yet"}
        else:
            point = _point(prior, x, baseline, index, cutoff)
            if not trading and point["status"] == "available":
                point["status"] = "non_session_carry"
        point["session"] = trading
        output.append(point)
        cursor += timedelta(days=1)
    return output


def _period_row(
    *,
    year: int,
    group: str,
    start: date,
    end: date,
    view_end: date,
    index: dict,
    cutoff: date,
    name: str,
    monthly: bool,
    alignment: str,
) -> tuple[dict, list[dict]]:
    full_sessions = sessions(start, end, name)
    active_end = min(end, view_end, cutoff)
    expected = [day for day in full_sessions if day <= active_end]
    baseline_date = (
        previous_session(start, name) if monthly else (full_sessions[0] if full_sessions else None)
    )
    baseline = _price(index, baseline_date, cutoff) if baseline_date is not None else None
    prices = [_price(index, day, cutoff) for day in expected]
    valid = sum(price is not None for price in prices)
    complete = bool(expected) and baseline is not None and valid == len(expected)
    if not full_sessions:
        status = "no_sessions"
    elif not expected:
        status = "not_started" if start > cutoff else "not_yet_formed"
    elif baseline is None:
        status = "missing_baseline"
    elif not complete:
        status = "incomplete_path"
    else:
        status = "available" if end <= active_end else "in_progress"
    end_price = prices[-1] if prices else None
    normalized = [endpoint_change(baseline, price).value for price in prices]
    row = {
        "year": year,
        "group": group,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "actual_start": expected[0].isoformat() if expected else None,
        "actual_end": expected[-1].isoformat() if expected else None,
        "baseline_date": baseline_date.isoformat() if baseline_date else None,
        "endpoint": _ratio(baseline, end_price),
        "relative_high": _text(max(normalized)) if complete else None,
        "relative_low": _text(min(normalized)) if complete else None,
        # Monthly returns include the prior month close; drawdown needs that same
        # starting peak. Interval returns begin at their own first close.
        "max_drawdown": _text(maximum_drawdown([baseline, *prices] if monthly else prices).value)
        if complete
        else None,
        "expected_sessions": len(expected),
        "valid_sessions": valid,
        "full_period_sessions": len(full_sessions),
        "complete": complete,
        "period_ended": end <= cutoff,
        "status": status,
        "missing_dates": [day.isoformat() for day, price in zip(expected, prices) if price is None],
        "single_session": len(expected) == 1,
    }
    if active_end < start:
        points = []
    elif alignment == "calendar":
        points = _calendar_points(
            start, active_end, expected, baseline_date, baseline, index, cutoff, monthly=monthly
        )
        if monthly:
            points.insert(
                0,
                {
                    "x": 0,
                    "date": baseline_date.isoformat(),
                    "value": "0" if baseline else None,
                    "status": "available" if baseline else "missing_baseline",
                    "session": True,
                },
            )
    else:
        points = [
            _point(day, i + int(monthly), baseline, index, cutoff) for i, day in enumerate(expected)
        ]
        if monthly:
            points.insert(
                0,
                {
                    "x": 0,
                    "date": baseline_date.isoformat(),
                    "value": "0" if baseline else None,
                    "status": "available" if baseline else "missing_baseline",
                    "session": True,
                },
            )
    return row, points


def _same_progress_end(
    start: date,
    end: date,
    current_start: date,
    current_end: date,
    cutoff: date,
    name: str,
    alignment: str,
) -> date:
    if cutoff < current_start:
        return start - timedelta(days=1)
    if cutoff >= current_end:
        return end
    if alignment == "trading":
        count = len(sessions(current_start, cutoff, name))
        available = sessions(start, end, name)
        return (
            available[min(count, len(available)) - 1]
            if count and available
            else (start - timedelta(days=1))
        )
    # Map month-day AND the cross-year position, not a fixed elapsed-day count:
    # the latter is one day late after February in a leap-year comparison.
    mapped = _mapped_day(start.year + cutoff.year - current_start.year, (cutoff.month, cutoff.day))
    return min(end, mapped)


def _period_research(params: dict, index: dict, today: date, cutoff: date) -> dict:
    kind = params.get("kind", "monthly")
    name = params.get("calendar", "XNYS")
    alignment = params.get("alignment", "calendar")
    if alignment not in {"calendar", "trading"}:
        raise ValueError("alignment must be calendar or trading")
    if kind == "monthly":
        current = _year(params.get("current_year", today.year))
        month = int(params.get("month", today.month))
        if not 1 <= month <= 12:
            raise ValueError("month must be between 1 and 12")

        def bounds(year):
            return date(year, month, 1), date(year, month, gregorian.monthrange(year, month)[1])
    else:
        start_md, end_md, cross, current = _interval_rules(params, today)

        def bounds(year):
            return _mapped_day(year, start_md), _mapped_day(year + int(cross), end_md)

    years, exclusions = _selected_years(params, current)
    current_start, current_end = bounds(current)
    comparison = params.get(
        "comparison", "same_progress" if current_start <= cutoff < current_end else "complete"
    )
    if comparison not in {"same_progress", "complete"}:
        raise ValueError("comparison must be same_progress or complete")
    rows, series, cells = [], [], []
    for year in [*years, current]:
        group = "current" if year == current else "historical"
        start, end = bounds(year)
        view_end = end
        if comparison == "same_progress" and group == "historical":
            view_end = _same_progress_end(
                start, end, current_start, current_end, cutoff, name, alignment
            )
        row, points = _period_row(
            year=year,
            group=group,
            start=start,
            end=end,
            view_end=view_end,
            index=index,
            cutoff=cutoff,
            name=name,
            monthly=kind == "monthly",
            alignment=alignment,
        )
        row["eligible"] = (
            group == "historical"
            and row["complete"]
            and (comparison == "same_progress" or row["period_ended"])
        )
        if kind == "monthly":
            row["month"] = month
        rows.append(row)
        series.append(
            {
                "key": str(year),
                "label": str(year) if start.year == end.year else f"{year}—{end.year}",
                "year": year,
                "group": group,
                "points": points,
            }
        )
        if group == "historical" and not row["eligible"]:
            exclusions.append(
                {"year": year, "reason": row["status"], "missing_dates": row["missing_dates"]}
            )
        if kind == "monthly":
            for cell_month in range(1, 13):
                cell_start = date(year, cell_month, 1)
                cell_end = date(year, cell_month, gregorian.monthrange(year, cell_month)[1])
                cell, _ = _period_row(
                    year=year,
                    group=group,
                    start=cell_start,
                    end=cell_end,
                    view_end=cell_end,
                    index=index,
                    cutoff=cutoff,
                    name=name,
                    monthly=True,
                    alignment="trading",
                )
                cell["month"] = cell_month
                cells.append(cell)
    eligible = [row for row in rows if row["eligible"]]
    summary = _statistics([row["endpoint"] for row in eligible])
    summary["current"] = next(row for row in rows if row["group"] == "current")
    summary["path"] = _path_statistics(series)
    summary["worst_drawdown"] = _text(
        max(
            (Decimal(row["max_drawdown"]) for row in eligible if row["max_drawdown"] is not None),
            default=None,
        )
    )
    if kind == "monthly":
        summary["months"] = [
            {
                "month": cell_month,
                **_statistics(
                    [
                        cell["endpoint"]
                        for cell in cells
                        if cell["month"] == cell_month
                        and cell["group"] == "historical"
                        and cell["complete"]
                        and cell["period_ended"]
                    ]
                ),
            }
            for cell_month in range(1, 13)
        ]
    return {
        "kind": kind,
        "metadata": {
            "params": params,
            "historical_years": years,
            "current_year": current,
            "target_n": len(years),
            "cutoff_date": cutoff.isoformat(),
            "calendar": name,
            "calendar_version": calendar_version(),
            "alignment": alignment,
            "comparison": comparison,
        },
        "summary": summary,
        "cells": cells,
        "series": series,
        "rows": rows,
        "effective_n": len(eligible),
        "exclusions": exclusions,
    }


def compute_research(
    params: dict, bars: list[dict], today: date | None = None,
    benchmark: dict | None = None,
) -> dict:
    """Compute one security using a single qualified price/input version."""
    params = {key: value for key, value in params.items() if value is not None}
    as_of = today or datetime.now(ET).date()
    cutoff = completed_through(today, params.get("calendar", "XNYS"))
    index = _bars_by_date(bars)
    kind = params.get("kind", "monthly")
    if kind in {"monthly", "interval"}:
        result = _period_research(params, index, as_of, cutoff)
    else:
        raise ValueError(f"Unsupported research kind: {kind}")
    add_distributions(result, benchmark)
    return ResearchResult.model_validate(result).model_dump(mode="json")


def transaction_price_context(
    bars: list[dict],
    transaction_date: str | date,
    accepted_at: str | datetime | None,
    today: date | None = None,
    calendar: str = "XNYS",
) -> dict:
    """Two explicitly different day-zero conventions and next-open observation."""
    index = _bars_by_date(bars)
    cutoff = completed_through(today, calendar)
    original = (
        date.fromisoformat(transaction_date)
        if isinstance(transaction_date, str)
        else transaction_date
    )
    transaction = next_session(original, calendar)

    def context(anchor: date, before: int = 5, after: int = 5) -> dict:
        days = session_window(anchor, before, after, calendar)
        baseline = _price(index, anchor, cutoff)
        future = days[before + 1 :]
        path_prices = [_price(index, day, cutoff) for day in days if day <= cutoff]
        path_complete = all(price is not None for price in path_prices)
        return {
            "baseline_date": anchor.isoformat(),
            "baseline_close": _text(baseline),
            "pre_5": _ratio(_price(index, days[0], cutoff), baseline),
            "day_1": _ratio(baseline, _price(index, future[0], cutoff)),
            "day_5": _ratio(baseline, _price(index, future[4], cutoff)),
            "day_1_date": future[0].isoformat(),
            "day_5_date": future[4].isoformat(),
            "mature_sessions": sum(day <= cutoff for day in future),
            "status": "not_yet_formed"
            if anchor > cutoff
            else (
                "missing_baseline"
                if baseline is None
                else "available"
                if path_complete
                else "incomplete_path"
            ),
            "day_1_status": "not_yet_formed"
            if future[0] > cutoff
            else (
                "available" if baseline and _price(index, future[0], cutoff) else "missing_price"
            ),
            "day_5_status": "not_yet_formed"
            if future[4] > cutoff
            else (
                "available" if baseline and _price(index, future[4], cutoff) else "missing_price"
            ),
            "points": [
                {
                    **_point(day, i - before, baseline, index, cutoff),
                    "close": _text(_price(index, day, cutoff)),
                }
                for i, day in enumerate(days)
                if day <= cutoff
            ],
        }

    left = {
        **context(transaction),
        "title": "交易日收盘前后股价",
        "original_date": original.isoformat(),
        "observation_anchor": transaction.isoformat(),
        "non_session_transaction": transaction != original,
    }
    right = None
    next_open = None
    date_only_acceptance = isinstance(accepted_at, str) and len(accepted_at) == 10
    if accepted_at is not None:
        reaction = reaction_session(accepted_at, calendar=calendar)
        baseline = date.fromisoformat(reaction["baseline_date"])
        reaction_day = date.fromisoformat(reaction["reaction_date"])
        observation = context(baseline)
        right = {
            **observation,
            **reaction,
            "price_status": observation["status"],
            "title": "披露日期观察" if date_only_acceptance else "披露前最后收盘归零",
            "opening_gap": _ratio(
                _price(index, baseline, cutoff), _price(index, reaction_day, cutoff, "open")
            )
            if reaction["opening_attribution"]
            else None,
        }
    if accepted_at is not None and not date_only_acceptance:
        starting = next_regular_open_after(accepted_at, calendar)
        days = session_window(starting, 0, 4, calendar)
        opening = _price(index, starting, cutoff, "open")
        next_open = {
            "title": "从公开后下一次常规开盘观察",
            "start_date": starting.isoformat(),
            "opening_price": _text(opening),
            "day_1": _ratio(opening, _price(index, days[0], cutoff)),
            "day_5": _ratio(opening, _price(index, days[4], cutoff)),
            "day_1_date": days[0].isoformat(),
            "day_5_date": days[4].isoformat(),
            "mature_sessions": sum(day <= cutoff for day in days),
            "day_1_status": "not_yet_formed"
            if days[0] > cutoff
            else ("available" if opening and _price(index, days[0], cutoff) else "missing_price"),
            "day_5_status": "not_yet_formed"
            if days[4] > cutoff
            else ("available" if opening and _price(index, days[4], cutoff) else "missing_price"),
        }
    return {
        "transaction": left,
        "disclosure": right,
        "next_open": next_open,
        "cutoff_date": cutoff.isoformat(),
        "calendar": calendar,
        "calendar_version": calendar_version(),
        "calculation_version": CALCULATION_VERSION,
        "accepted_at": as_et(accepted_at).isoformat()
        if accepted_at is not None and not date_only_acceptance
        else None,
        "accepted_date": as_et(accepted_at).date().isoformat() if accepted_at is not None else None,
        "calendar_days_to_disclosure": (as_et(accepted_at).date() - original).days
        if accepted_at
        else None,
        "price_basis": "split_only",
        "disclosure_status": "available" if right else "missing_acceptance_time",
    }
