"""Calendar-aware research from one already-qualified price version.

This module neither fetches nor adjusts prices. Ratios are decimal strings (not
percent points). Only bars marked VALID enter calculations, and every expected
session remains represented even when its price is missing. The returned object
is shared by graphs, tables and exports; a frontend must not recompute metrics.
"""

import calendar as gregorian
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Literal

from pydantic import BaseModel, Field

from iirp.analytics.calendar import (
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
from iirp.analytics.distributions import (
    Distribution,
    MonthlyRanking,
    ResearchMethodology,
    add_distributions,
    qualify_common_quarters,
    statistics,
)
from iirp.analytics.event_dates import analyze_event_dates
from iirp.analytics.fiscal_calendar import FiscalCoverage
from iirp.analytics.prices import endpoint_change, maximum_drawdown

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
    kind: Literal["monthly", "interval", "earnings"]
    metadata: ResearchMetadata
    summary: dict[str, Any]
    cells: list[dict[str, Any]]
    series: list[ResearchSeries]
    rows: list[dict[str, Any]]
    effective_n: int
    exclusions: list[dict[str, Any]]
    date_observation: dict[str, Any] | None = None
    distributions: list[Distribution] = Field(default_factory=list)
    benchmark: dict[str, Any] | None = None
    monthly_rankings: list[MonthlyRanking] = Field(default_factory=list)
    fiscal_coverage: FiscalCoverage | None = None


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


def _current_fiscal_year(params: dict, today: date) -> int | None:
    explicit = params.get("current_fiscal_year", params.get("current_year"))
    if explicit is not None:
        return _year(explicit)
    if params.get("fiscal_year_end_mmdd"):
        boundary = _mmdd(params["fiscal_year_end_mmdd"])
        return today.year + int((today.month, today.day) > boundary)
    return None


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
    """Inclusive collection bounds, including needed baseline, never future.

    Earnings price bounds require actual discovered events in params['events'];
    fiscal years cannot be safely translated to a fixed natural-year interval.
    """
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
    elif kind == "earnings":
        ranges = []
        for event in params.get("events", []):
            anchor = reaction_session(
                event.get("announced_at"),
                announced_date=event.get("announced_date"),
                time_precision=event.get("time_precision", "date_only"),
                calendar=name,
            )
            if anchor["baseline_date"]:
                baseline = date.fromisoformat(anchor["baseline_date"])
                days = session_window(baseline, 20, 60, name)
                ranges.append((days[0], min(today, days[-1])))
        if not ranges:
            raise ValueError("Discover fiscal earnings events before planning their price windows")
        start, end = min(pair[0] for pair in ranges), max(pair[1] for pair in ranges)
    else:
        raise ValueError(f"Unsupported research kind: {kind}")
    if start > end:
        raise ValueError("The requested range has not started; no price collection is due")
    return start, end


def _quantile(values: list[Decimal], quantile: Decimal) -> Decimal | None:
    if not values:
        return None
    values = sorted(values)
    with localcontext() as context:
        context.prec = 34
        position = Decimal(len(values) - 1) * quantile
        low = int(position)
        fraction = position - low
        return values[low] + fraction * (values[min(low + 1, len(values) - 1)] - values[low])


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


def _event_row(event: dict, index: dict, cutoff: date, name: str) -> tuple[dict, list[dict]]:
    anchor = reaction_session(
        event.get("announced_at"),
        announced_date=event.get("announced_date"),
        time_precision=event.get("time_precision", "date_only"),
        calendar=name,
    )
    row = {
        "event_id": str(event.get("id", "")),
        "year": event.get("fiscal_year"),
        "fiscal_year": event.get("fiscal_year"),
        "quarter": event.get("fiscal_quarter"),
        "announced_at": event.get("announced_at"),
        "announced_date": event.get("announced_date"),
        "time_precision": event.get("time_precision", "date_only"),
        "verified": event.get("verified") is True,
        **anchor,
        "pre_20": None,
        "opening_gap": None,
        "windows": {},
    }
    fact = earnings_date_event(event, cutoff)
    row.update(period_start=fact.get("period_start"), period_end=fact.get("period_end"))
    row["precise"] = bool(anchor["opening_attribution"] and row["verified"]
        and fact["date_verified"] and fact["time_verified"] and fact["period_verified"]
        and fact["period_kind"] == "regular" and fact["event_status"] == "occurred" and not fact["excluded"])
    if not row["verified"]:
        row["status"] = "unverified_event"
    if not anchor["baseline_date"]:
        for window in WINDOWS:
            row["windows"][str(window)] = {
                "day": window,
                "cumulative": None,
                "after_open": None,
                "status": "missing_event_date",
                "complete": False,
                "eligible": False,
            }
        return row, []
    baseline_date = date.fromisoformat(anchor["baseline_date"])
    days = session_window(baseline_date, 20, 60, name)
    baseline = _price(index, baseline_date, cutoff)
    reaction_date = days[21]
    opening = _price(index, reaction_date, cutoff, "open")
    row["pre_20"] = _ratio(_price(index, days[0], cutoff), baseline)
    row["opening_gap"] = _ratio(baseline, opening) if row["precise"] else None
    row["mature_sessions"] = sum(day <= cutoff for day in days[21:])
    for window in WINDOWS:
        selected = days[21 : 21 + window]
        closes = [_price(index, day, cutoff) for day in selected]
        mature = selected[-1] <= cutoff
        complete = mature and baseline is not None and all(price is not None for price in closes)
        status = (
            "not_yet_formed"
            if not mature
            else "missing_baseline"
            if baseline is None
            else "incomplete_path"
            if not complete
            else "available"
            if row["precise"]
            else "unconfirmed_event_time"
        )
        row["windows"][str(window)] = {
            "day": window,
            "start_date": baseline_date.isoformat(),
            "end_date": selected[-1].isoformat(),
            "cumulative": _ratio(baseline, closes[-1]) if mature else None,
            "after_open": _ratio(opening, closes[-1]) if mature and row["precise"] else None,
            "status": status,
            "complete": complete,
            "max_drawdown": _text(maximum_drawdown([baseline, *closes]).value) if complete else None,
            "eligible": complete and row["precise"],
            "missing_dates": [
                day.isoformat()
                for day, close in zip(selected, closes)
                if day <= cutoff and close is None
            ],
        }
    points = [
        _point(day, i - 20, baseline, index, cutoff) for i, day in enumerate(days) if day <= cutoff
    ]
    return row, points


def earnings_date_event(event: dict, as_of: date) -> dict:
    """Adapt a server earnings fact, never a supplier's guessed clock.

    Legacy verified means the date and fiscal identity were reviewed together.
    Explicit newer review flags can only narrow that qualification here. A
    period-only clock stays a period; only an aware actual instant becomes HH:MM.
    """
    raw_date = event.get("announced_date")
    try:
        day = date.fromisoformat(str(raw_date)) if raw_date is not None else None
    except ValueError:
        day = None
    precision = event.get("time_precision", "date_only")
    conflict = precision == "conflict" or event.get("status") == "CONFLICT"
    date_verified = (
        event.get("date_verified", event.get("verified")) is True
        and not conflict
        and day is not None
    )
    fiscal_year, fiscal_quarter = event.get("fiscal_year"), event.get("fiscal_quarter")
    period_verified = bool(
        event.get("fiscal_period_verified", event.get("verified")) is True
        and fiscal_year is not None
        and fiscal_quarter in (1, 2, 3, 4)
    )
    actual_time, zone, time_basis, time_verified = None, None, "unknown", False
    release_session = "unknown"
    if precision == "exact" and event.get("announced_at") and date_verified:
        stamp = event["announced_at"]
        try:
            stamp = (
                datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if isinstance(stamp, str)
                else stamp
            )
            if stamp.tzinfo is not None and stamp.utcoffset() is not None:
                stamp = stamp.astimezone(ET)
                if day is not None and stamp.date() != day:
                    conflict, date_verified = True, False
                elif event.get("precise_time_supported", event.get("verified")) is True:
                    actual_time, zone = stamp.strftime("%H:%M"), "America/New_York"
                    time_basis, time_verified = "reported_actual", True
        except (ValueError, AttributeError):
            pass
    elif precision in {"before_open", "after_close", "intraday"} and date_verified:
        # 'precise_time_supported' denotes opening attribution, so it is false
        # for a verified intraday period even though that period is known.
        if (
            precision == "intraday"
            or event.get("precise_time_supported", event.get("verified")) is True
        ):
            release_session = "during_session" if precision == "intraday" else precision
            time_verified = True
    evidence = [
        item
        for item in (event.get("evidence") or [])
        if isinstance(item, dict) and not item.get("rejected")
    ]
    # An EX99 release can receive its date from the containing 8-K Item 2.02.
    # Keep both raw objects, but attribute that date to the 8-K itself.
    for item in list(evidence):
        contexts = item.get("announcement_date_evidence") or []
        context_url = item.get("announcement_context_source_url")
        if context_url and isinstance(contexts, list):
            for context in contexts:
                if isinstance(context, dict) and context.get("announced_date") == item.get("announced_date"):
                    evidence.append({
                        "provider": "SEC announcement context",
                        "source_url": context_url,
                        "announced_date": context["announced_date"],
                        "fiscal_year": item.get("fiscal_year"),
                        "fiscal_quarter": item.get("fiscal_quarter"),
                        "excerpt": context.get("excerpt"),
                        "source_hash": item.get("source_hash"),
                    })
    from iirp.earnings_data import validate_source_url

    def source_link_ok(candidate):
        try:
            validate_source_url(candidate.get("source_url"))
        except ValueError:
            return False
        return True

    def ambiguous_legacy(candidate):
        return (
            candidate.get("provider") == "manual_review"
            and candidate.get("period_kind") in {"regular", "transition"}
        )

    date_claimed = any(
        source_link_ok(item)
        and not ambiguous_legacy(item)
        and not item.get("announcement_date_evidence")
        and day is not None
        and str(item.get("announced_date")) == day.isoformat()
        and item.get("fiscal_year", fiscal_year) == fiscal_year
        and item.get("fiscal_quarter", fiscal_quarter) == fiscal_quarter
        for item in evidence
    )
    if date_verified and not date_claimed:
        date_verified = False
        actual_time, zone, time_basis, time_verified = None, None, "unknown", False
        release_session = "unknown"

    def time_source_matches(candidate):
        if (
            not source_link_ok(candidate)
            or ambiguous_legacy(candidate)
            or not isinstance(candidate.get("time_evidence"), str)
            or not candidate["time_evidence"].strip()
            or day is None
            or str(candidate.get("announced_date")) != day.isoformat()
            or candidate.get("fiscal_year") != fiscal_year
            or candidate.get("fiscal_quarter") != fiscal_quarter
            or candidate.get("time_precision") != precision
        ):
            return False
        if precision == "exact":
            try:
                claim = datetime.fromisoformat(str(candidate.get("announced_at")).replace("Z", "+00:00"))
                actual = datetime.fromisoformat(str(event.get("announced_at")).replace("Z", "+00:00"))
                return claim.tzinfo is not None and actual.tzinfo is not None and claim == actual
            except ValueError:
                return False
        return precision in {"before_open", "after_close"}

    time_claimed = date_verified and time_verified and any(time_source_matches(item) for item in evidence)
    if not time_claimed:
        actual_time, zone, time_basis, time_verified = None, None, "unknown", False
        release_session = "unknown"
    sources = []
    period_dates = set()
    period_starts = set()
    for candidate in [event, *evidence]:
        if not candidate.get("period_end"):
            continue
        if (
            fiscal_year is None
            or fiscal_quarter not in (1, 2, 3, 4)
            or candidate.get("fiscal_year") != fiscal_year
            or candidate.get("fiscal_quarter") != fiscal_quarter
        ):
            continue
        try:
            period_end = date.fromisoformat(str(candidate["period_end"]))
            if day is not None and period_end <= day:
                period_dates.add(period_end.isoformat())
                if candidate.get("period_start"):
                    start = date.fromisoformat(str(candidate["period_start"]))
                    if start <= period_end:
                        period_starts.add(start.isoformat())
        except ValueError:
            pass
    # Fiscal year/quarter review alone does not prove a regular reporting
    # period. Require an explicit, linked source claim for this exact period;
    # conflicting or missing claims remain unknown.
    period_reviews = [item for item in evidence if item.get("provider") == "manual_period_review"]
    # Older one-link manual reviews can conflate the announcement and a later
    # 10-Q. They need a new field-specific review before qualifying as regular.
    kind_candidates = [period_reviews[-1]] if period_reviews else [
        item for item in evidence if item.get("provider") != "manual_review"
    ]
    known_kinds = {
        item["period_kind"]
        for item in kind_candidates
        if item.get("period_kind") in {"regular", "transition"}
        and isinstance(item.get("period_kind_evidence"), str)
        and bool(item["period_kind_evidence"].strip())
        and source_link_ok(item)
        and fiscal_year is not None
        and fiscal_quarter in (1, 2, 3, 4)
        and item.get("fiscal_year") == fiscal_year
        and item.get("fiscal_quarter") == fiscal_quarter
    }
    period_kind = next(iter(known_kinds)) if len(known_kinds) == 1 else "unknown"
    estimated = event.get("is_estimate") is True
    event_status = (
        "scheduled"
        if estimated
        else "occurred"
        if day is not None and day <= as_of and date_verified
        else "unknown"
    )
    excluded = bool(
        event.get("excluded")
        or event.get("is_primary") is False
        or event.get("status") in {"EXCLUDED", "SECONDARY", "DUPLICATE_CONFIRMED"}
    )
    # A source URL alone does not attest to every fiscal field. Match each
    # source's explicit values to this version, never a manual review's previous.
    for candidate in evidence:
        if not source_link_ok(candidate):
            continue
        supports = set()
        ambiguous_legacy_review = ambiguous_legacy(candidate)
        date_scope_match = all(candidate.get(field, value) == value for field, value in
                               (("fiscal_year", fiscal_year), ("fiscal_quarter", fiscal_quarter)))
        if not ambiguous_legacy_review and not candidate.get("announcement_date_evidence") and date_scope_match and day is not None and str(candidate.get("announced_date")) == day.isoformat():
            supports.add("event_date")
        if time_claimed and time_source_matches(candidate):
            supports.add("event_time" if precision == "exact" else "release_session")
        explicit_period_match = (
            fiscal_year is not None
            and fiscal_quarter in (1, 2, 3, 4)
            and candidate.get("fiscal_year") == fiscal_year
            and candidate.get("fiscal_quarter") == fiscal_quarter
        )
        if explicit_period_match:
            if period_kind != "unknown" and candidate in kind_candidates and candidate.get("period_kind") == period_kind:
                supports.add("period_kind")
            for field, known in (("period_start", period_starts), ("period_end", period_dates)):
                if len(known) == 1 and str(candidate.get(field)) in known:
                    supports.add(field)
        kind_evidence = (
            candidate.get("period_kind_evidence")
            if "period_kind" in supports else None
        )
        other_note = (
            "旧版单来源同时标注公告与财期，来源支持存在歧义；须分别复核"
            if ambiguous_legacy_review
            else candidate.get("note") or candidate.get("excerpt")
        )
        note_parts = [
            f"财期类型依据：{kind_evidence}" if kind_evidence else None,
            f"实际公开时刻依据：{candidate.get('time_evidence')}" if "event_time" in supports or "release_session" in supports else None,
            other_note if other_note != kind_evidence else None,
        ]
        sources.append({
            "url": candidate["source_url"],
            "title": candidate.get("title") or candidate.get("provider") or "来源",
            "evidence_note": "；".join(part for part in note_parts if part) or None,
            "period_kind_evidence": kind_evidence,
            "supports": sorted(supports),
        })
    return {
        "client_event_id": str(event.get("id", "")),
        "first_observed_at": event.get("first_observed_at"),
        "review": {"confirmed_at": event.get("last_verified_at")} if date_verified else {},
        "event_name": f"FY{fiscal_year} Q{fiscal_quarter} 业绩发布"
        if fiscal_year is not None and fiscal_quarter is not None
        else "财期待核对的业绩发布日期",
        "event_type": "earnings_release",
        "event_date": day.isoformat() if day is not None and not conflict else None,
        "event_year": day.year if day is not None and not conflict else None,
        "event_time": actual_time if not conflict else None,
        "timezone": zone if not conflict else None,
        "time_precision": "unknown"
        if day is None or conflict
        else "minute"
        if actual_time
        else "date",
        "time_basis": time_basis if not conflict else "unknown",
        "release_session": release_session if not conflict else "unknown",
        "event_status": event_status,
        "date_status": "conflicting"
        if conflict
        else "supported"
        if date_verified
        else "unverified",
        "date_verified": date_verified,
        "time_verified": time_verified and not conflict,
        "fiscal_year": fiscal_year,
        "fiscal_quarter": fiscal_quarter,
        "period_end": next(iter(period_dates)) if len(period_dates) == 1 else None,
        "period_start": next(iter(period_starts)) if len(period_starts) == 1 else None,
        "period_kind": period_kind,
        "period_verified": period_verified,
        "excluded": excluded,
        "sources": sources,
        "calendar_conflicts": [field for field, values in
                               (("period_start", period_starts), ("period_end", period_dates)) if len(values) > 1],
    }


def _earnings_date_observation(
    params: dict, index: dict, events: list[dict], today: date, cutoff: date
) -> dict:
    current = _current_fiscal_year(params, today)
    years, _ = _selected_years(params, current)
    excluded_years = set(params.get("excluded_years") or [])
    quarter = int(params.get("quarter", 1))
    selected = []
    for event in events:
        fiscal_year = event.get("fiscal_year")
        if fiscal_year in excluded_years:
            continue
        if fiscal_year is not None:
            if current is not None and fiscal_year not in [*years, current]:
                continue
            if current is None and params.get("years") is not None and fiscal_year not in years:
                continue
        # Unknown fiscal identity remains a labelled candidate observation. It
        # never joins the requested standard quarter's default sample count.
        selected.append(earnings_date_event(event, today))
    bars = [
        {"date": day.isoformat(), "close": value["close"], "status": "VALID"}
        for day, value in index.items()
    ]
    return analyze_event_dates(
        selected,
        bars,
        cutoff=cutoff,
        current_year=today.year,
        current_fiscal_year=current,
        calendar=params.get("calendar", "XNYS"),
        metadata={
            "research_kind": "earnings",
            "requested_fiscal_years": years,
            "common_years": params.get("common_years", False),
            "quarter": quarter,
            "params": params,
            "parent_calculation_version": CALCULATION_VERSION,
            "warnings": [
                "日期观察与精确反应分别计算样本数量；未知财期候选不归入所选标准季度",
                "候选日期仅供核对，不因已有行情而升级为实际发布事实",
            ],
        },
    )


def _earnings_research(
    params: dict, index: dict, events: list[dict], today: date, cutoff: date
) -> dict:
    name = params.get("calendar", "XNYS")
    current = _current_fiscal_year(params, today)
    years, exclusions = _selected_years(params, current)
    quarter = int(params.get("quarter", 1))
    window = int(params.get("window", 5))
    if quarter not in (1, 2, 3, 4) or window not in WINDOWS:
        raise ValueError("quarter must be 1–4 and window must be 1, 5, 20 or 60")
    warnings = []
    if current is None:
        warnings.append("current_fiscal_year_unconfirmed")
        # Date observations remain useful, but cannot become a falsely labelled
        # historical or current-fiscal-year comparison.
    groups = defaultdict(list)
    for event in events:
        if event.get("fiscal_year") is not None and event.get("fiscal_quarter") in (1, 2, 3, 4):
            groups[(int(event["fiscal_year"]), int(event["fiscal_quarter"]))].append(event)
        else:
            exclusions.append({"event_id": event.get("id"), "reason": "fiscal_period_unconfirmed"})
    display_years = sorted({year for year, _ in groups}) if current is None else [*years, current]
    rows, cells, series = [], [], []
    for year in display_years:
        group = "observation" if current is None else "current" if year == current else "historical"
        for fiscal_quarter in (1, 2, 3, 4):
            candidates = groups.get((year, fiscal_quarter), [])
            # Parent may provide candidate duplicates or call this before main
            # event selection; never silently pick a favourable observation.
            primaries = [event for event in candidates if event.get("is_primary", True)]
            if len(primaries) != 1:
                reason = "missing_event" if not primaries else "duplicate_primary_events"
                cell = {
                    "year": year,
                    "fiscal_year": year,
                    "quarter": fiscal_quarter,
                    "group": group,
                    "endpoint": None,
                    "status": reason,
                    "eligible": False,
                }
                cells.append(cell)
                if fiscal_quarter == quarter:
                    rows.append({**cell, "windows": {}})
                    exclusions.append({"year": year, "quarter": fiscal_quarter, "reason": reason})
                continue
            row, points = _event_row(primaries[0], index, cutoff, name)
            row["group"] = group
            for result in row["windows"].values():
                result["eligible"] = result["eligible"] and group == "historical"
            selected = row["windows"][str(window)]
            row["endpoint"] = selected["cumulative"]
            row["eligible"] = selected["eligible"]
            row["window_status"] = selected["status"]
            cells.append({**row, "window": window})
            if fiscal_quarter == quarter:
                rows.append(row)
                series.append(
                    {
                        "key": str(row["event_id"]),
                        "label": f"FY{year} Q{quarter}",
                        "year": year,
                        "group": group,
                        "points": points,
                    }
                )
                if not row["eligible"]:
                    exclusions.append(
                        {
                            "year": year,
                            "quarter": quarter,
                            "event_id": row["event_id"],
                            "reason": "current_fiscal_year"
                            if group == "current"
                            else selected["status"],
                        }
                    )
    qualify_common_quarters(cells, params.get("common_years", False))
    for row in [*rows, *cells]:
        row["eligible"] = row.get("windows", {}).get(str(window), {}).get("eligible", False)
    window_summaries = {}
    for item in WINDOWS:
        key = str(item)
        eligible = [row for row in rows if row.get("windows", {}).get(key, {}).get("eligible")]
        window_summaries[key] = {
            **_statistics([row["windows"][key]["cumulative"] for row in eligible]),
            "opening_gap": _statistics([row["opening_gap"] for row in eligible]),
            "after_open": _statistics([row["windows"][key]["after_open"] for row in eligible]),
        }
    summary = {**window_summaries[str(window)], "windows": window_summaries}
    # Unreliable dates may be drawn individually but never enter precise bands.
    precise_ids = {str(row.get("event_id")) for row in rows if row.get("precise")}
    if params.get("common_years"):
        precise_ids &= {str(row.get("event_id")) for row in rows if row.get("eligible")}
    summary["path"] = _path_statistics([item for item in series if item["key"] in precise_ids])
    summary["current"] = next((row for row in rows if row["group"] == "current"), None)
    return {
        "kind": "earnings",
        "date_observation": _earnings_date_observation(params, index, events, today, cutoff),
        "metadata": {
            "params": params,
            "historical_years": years,
            "current_year": current,
            "target_n": len(years)
            if current is not None
            else int(params.get("historical_years", 8)),
            "cutoff_date": cutoff.isoformat(),
            "calendar": name,
            "calendar_version": calendar_version(),
            "alignment": "trading",
            "comparison": "same_fiscal_quarter",
            "warnings": warnings,
        },
        "summary": summary,
        "cells": cells,
        "series": series,
        "rows": rows,
        "effective_n": summary["n"],
        "exclusions": exclusions,
    }


def compute_research(
    params: dict, bars: list[dict], events: list[dict] | None = None, today: date | None = None,
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
    elif kind == "earnings":
        result = _earnings_research(params, index, events or [], as_of, cutoff)
    else:
        raise ValueError(f"Unsupported research kind: {kind}")
    add_distributions(result, benchmark)
    if result.get("date_observation"):
        add_distributions(result["date_observation"], benchmark)
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
