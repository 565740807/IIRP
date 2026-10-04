"""Date observations around real events, independent of precise reaction windows.

D0 retains the exchange-local event date even after the close; non-session dates
map forward. Inputs are one qualified split-only price version and server-owned
review flags. No fetching, verification, adjustment, or event inference occurs.
"""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from iirp.analytics.calendar import (
    ET,
    calendar_version,
    next_session,
    session_bounds,
    session_window,
)
from iirp.analytics.distributions import add_distributions, qualify_common_quarters, statistics
from iirp.analytics.event_overlaps import REPRESENTATION_VERSION, summarize_overlaps
from iirp.analytics.prices import endpoint_change, maximum_drawdown

CALCULATION_VERSION = "event-dates-v7-fiscal-scope-evidence"
WINDOWS = {"before5": (-6, -1), "day0": (-1, 0), "after5": (0, 5), "through5": (-1, 5)}


def _day(value):
    return date.fromisoformat(value) if isinstance(value, str) else value


def resolve_event_anchor(event: dict, calendar: str = "XNYS") -> dict:
    """Return original date, exchange date, D0 and explicit timing qualifications."""
    original = _day(event.get("event_date"))
    result = {
        "original_date": original.isoformat() if original else None,
        "exchange_date": None,
        "anchor_date": None,
        "baseline_date": None,
        "first_subsequent_session": None,
        "non_session_shift": False,
        "date_alignment_approximate": False,
        "time_precision": event.get("time_precision", "unknown"),
        "time_basis": event.get("time_basis", "unknown"),
        "release_session": "unknown",
        "scheduled_session": "unknown",
        "warnings": [],
    }
    if original is None or event.get("date_status") == "conflicting":
        result["warnings"].append("事件日期缺失或冲突，未对齐交易日")
        return result
    # V1 supports US securities, consistently with the existing research calendar.
    exchange_day = original
    stamp = None
    if event.get("event_time") is not None and event.get("timezone"):
        stamp = (
            datetime.fromisoformat(f"{original}T{event['event_time']}")
            .replace(tzinfo=ZoneInfo(event["timezone"]))
            .astimezone(ET)
        )
        exchange_day = stamp.date()
    else:
        result["date_alignment_approximate"] = True
        result["warnings"].append("无分钟时刻，按日期观察；未补造午夜时间")
        if event.get("timezone") not in {None, "America/New_York"}:
            result["warnings"].append("活动与交易所时区可能跨日，请核对日期对齐")
    anchor = next_session(exchange_day, calendar)
    days = session_window(anchor, 6, 5, calendar)
    result.update(
        {
            "exchange_date": exchange_day.isoformat(),
            "anchor_date": anchor.isoformat(),
            "baseline_date": days[5].isoformat(),
            "first_subsequent_session": (anchor if anchor != exchange_day else days[7]).isoformat(),
            "non_session_shift": anchor != exchange_day,
        }
    )
    if anchor != exchange_day:
        result["warnings"].append("原日期休市，D0 顺延至首个交易日；原日期没有日线涨跌")
    timing = "unknown"
    if stamp is not None and anchor == exchange_day:
        opening, closing = session_bounds(anchor, calendar)
        timing = (
            "before_open"
            if stamp < opening
            else ("after_close" if stamp >= closing else "during_session")
        )
        if event.get("time_basis") == "official_schedule":
            result["scheduled_session"] = timing
            result["warnings"].append("时刻来自官方日程，时段仅为计划标记")
        elif event.get("time_verified") is True:
            result["release_session"] = timing
    if event.get("time_verified") is True and event.get("release_session", "unknown") != "unknown":
        claimed = event["release_session"]
        if timing != "unknown" and claimed != timing:
            result["release_session"] = "unknown"
            result["warnings"].append("已核对时段与时刻不一致，不能提供精确时段归因")
        elif anchor == exchange_day:
            result["release_session"] = claimed
    resolved = result["release_session"]
    if resolved == "after_close":
        result["warnings"].append("当日收盘早于事件，D0 涨跌发生在事件前；首个后续收盘在 D+1")
    elif result["scheduled_session"] == "after_close":
        result["warnings"].append("按官方日程，D0 收盘早于计划开始时刻；仍保留当天 D0")
    elif resolved == "during_session":
        result["warnings"].append("盘中事件：D0 整日涨跌含事件发生前的变化")
    return result


def event_price_scope(
    events: list[dict], cutoff: date, calendar: str = "XNYS"
) -> list[tuple[date, date]]:
    """Merge only selected, supported D-6..D+5 windows, never request future prices."""
    cutoff = _day(cutoff)
    ranges = []
    for event in events:
        if event.get("excluded") or event.get("date_status") != "supported":
            continue
        anchor = resolve_event_anchor(event, calendar)["anchor_date"]
        if not anchor:
            continue
        days = session_window(_day(anchor), 6, 5, calendar)
        if days[0] <= cutoff:
            ranges.append((days[0], min(days[-1], cutoff)))
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _prices(bars):
    output = {}
    for bar in bars:
        day = _day(bar["date"])
        if day in output:
            raise ValueError(f"Duplicate daily bar for {day}; select one published version first")
        close = None
        if bar.get("status") == "VALID":
            try:
                close = Decimal(str(bar["close"]))
                if not close.is_finite() or close <= 0:
                    close = None
            except (InvalidOperation, TypeError, KeyError):
                close = None
        output[day] = {"close": close, "status": bar.get("status", "MISSING")}
    return output


def _ratio(first, last):
    value = endpoint_change(first, last).value
    return str(value) if value is not None else None


def _metric(days, values, states, first, last, cutoff, eligible):
    selected = list(range(first, last + 1))
    mature = days[last] <= cutoff
    complete = mature and all(values[k] is not None for k in selected)
    if not mature:
        status = "not_yet_formed"
    elif any(states[k] == "before_listing" for k in selected):
        status = "before_listing"
    elif not complete:
        status = "incomplete_path"
    else:
        status = "available"
    return {
        "value": _ratio(values[first], values[last]),
        "status": status,
        "mature": mature,
        "complete": complete,
        "max_drawdown": str(maximum_drawdown([values[k] for k in selected]).value) if complete else None,
        "eligible": bool(eligible and complete),
        "start_date": days[first].isoformat(),
        "end_date": days[last].isoformat(),
        "expected_closes": len(selected),
        "available_closes": sum(values[k] is not None for k in selected),
    }


def _statistics(values):
    return statistics(values)


def analyze_event_dates(
    events: list[dict],
    bars: list[dict],
    *,
    cutoff: date,
    current_year: int,
    current_fiscal_year: int | None = None,
    calendar: str = "XNYS",
    metadata: dict | None = None,
    benchmark: dict | None = None,
    observed_event_ids: set[str] | None = None,
) -> dict:
    """Compute one frozen result for tables, charts and exports.

    Server review flags are date_verified, time_verified, period_verified and
    excluded. Only date-verified, occurred historical events enter summaries;
    earnings also require reviewed, regular fiscal identity. A complete price
    path cannot substitute for event review. Ratios remain decimal strings.
    """
    cutoff = _day(cutoff)
    metadata = dict(metadata or {})
    selected_window = metadata.get("params", {}).get("date_window", "after5")
    if selected_window not in WINDOWS:
        raise ValueError("不支持的日期观察窗口")
    window_start, window_end = WINDOWS[selected_window]
    if metadata.get("price_basis", "split_only") != "split_only":
        raise ValueError("Event date analysis requires one verified split_only price version")
    if len({event["client_event_id"] for event in events}) != len(events):
        raise ValueError("Event IDs must be unique")
    prices = _prices(bars)
    listing_date = _day(metadata.get("listing_date"))
    rows, cells, series, window_series, exclusions = [], [], [], [], []
    coverage_rows = []
    period_counts = {}
    def period_kind_supported(candidate):
        # Native earnings have a separate fiscal-fact review path. Imported
        # documents must retain an explicit source-to-period-kind claim, even
        # when an older saved review flag was set before this rule existed.
        return not metadata.get("event_set_id") or any(
            "period_kind" in source.get("supports", [])
            for source in candidate.get("sources", [])
            if isinstance(source, dict) and not source.get("rejected")
        )

    for candidate in events:
        if (
            candidate.get("event_type") == "earnings_release"
            and candidate.get("fiscal_year")
            and candidate.get("fiscal_quarter")
            and candidate.get("period_kind") == "regular"
            and period_kind_supported(candidate)
            and candidate.get("event_status") == "occurred"
            and not candidate.get("excluded")
        ):
            period = (candidate["fiscal_year"], candidate["fiscal_quarter"])
            period_counts[period] = period_counts.get(period, 0) + 1
    for event in events:
        key = event["client_event_id"]
        earnings = event.get("event_type") == "earnings_release"
        year = event.get("fiscal_year") if earnings else event.get("event_year")
        current = current_fiscal_year if earnings else current_year
        group = (
            "observation"
            if year is None or current is None
            else ("historical" if year < current else "current" if year == current else "scheduled")
        )
        category = (
            f"Q{event.get('fiscal_quarter')}"
            if earnings and event.get("fiscal_quarter")
            else ("unassigned_earnings" if earnings else event.get("event_type", "unknown"))
        )
        reasons = []
        if event.get("excluded"):
            reasons.append("user_excluded")
        params = metadata.get("params", {})
        if year in params.get("excluded_years", []) or (
            params.get("years") is not None and year is not None
            and year not in params["years"] and year != current
        ):
            reasons.append("outside_requested_years")
        if event.get("date_verified") is not True:
            reasons.append("unverified_event_date")
        if event.get("date_status") != "supported":
            reasons.append("unsupported_event_date")
        if event.get("event_status") != "occurred":
            reasons.append("event_not_confirmed_occurred")
        if group != "historical":
            reasons.append("not_historical_year" if group != "observation" else "unassigned_year")
        if earnings and (
            event.get("period_kind") != "regular"
            or not period_kind_supported(event)
            or not event.get("fiscal_year")
            or not event.get("fiscal_quarter")
            or event.get("period_verified") is not True
        ):
            reasons.append("unverified_or_nonstandard_fiscal_period")
        if (
            earnings
            and period_counts.get((event.get("fiscal_year"), event.get("fiscal_quarter")), 0) > 1
        ):
            reasons.append("duplicate_fiscal_period")
        anchor = resolve_event_anchor(event, calendar)
        row = {
            "key": key,
            "label": event["event_name"],
            "year": year,
            "event_year": event.get("event_year"),
            "fiscal_year": event.get("fiscal_year"),
            "fiscal_quarter": event.get("fiscal_quarter"),
            "period_end": str(event["period_end"]) if event.get("period_end") else None,
            "period_start": str(event["period_start"]) if event.get("period_start") else None,
            "category": category,
            "group": group,
            "event_status": event.get("event_status"),
            "date_status": event.get("date_status"),
            "date_verified": event.get("date_verified") is True,
            "time_verified": event.get("time_verified") is True,
            "period_verified": event.get("period_verified") is True,
            "period_kind": event.get("period_kind", "unknown"),
            "period_kind_source_supported": period_kind_supported(event) if earnings else None,
            "event_time": event.get("event_time"),
            "timezone": event.get("timezone"),
            "sources": event.get("sources", []),
            "review_note": event.get("review", {}).get("note"),
            "first_observed_at": event.get("first_observed_at"),
            "date_verified_at": event.get("review", {}).get("confirmed_at") if event.get("date_verified") is True else None,
            "calendar_conflicts": event.get("calendar_conflicts", []),
            "anchor": anchor,
            "points": [],
            "window_points": [],
            "windows": {},
            "eligible": False,
            "exclusion_reasons": reasons,
        }
        if observed_event_ids is not None and key not in observed_event_ids:
            # A source-only record has no price window/path. It still explains
            # coverage and keeps its dates, sources and actual review reasons.
            coverage_rows.append(row)
            continue
        if anchor["anchor_date"]:
            all_days = session_window(_day(anchor["anchor_date"]), 6, 5, calendar)
            days = {k: value for k, value in zip(range(-6, 6), all_days, strict=True)}
            values, states = {}, {}
            for k, day in days.items():
                item = prices.get(day, {})
                value = item.get("close") if day <= cutoff else None
                before_listing = (listing_date and day < listing_date) or item.get("status") in {
                    "BEFORE_LISTING",
                    "NOT_LISTED",
                }
                if before_listing:
                    value = None
                values[k] = value
                states[k] = (
                    "not_yet_formed"
                    if day > cutoff
                    else (
                        "before_listing"
                        if before_listing
                        else "available"
                        if value is not None
                        else "missing_price"
                    )
                )
            for name, (first, last) in WINDOWS.items():
                row["windows"][name] = _metric(
                    days, values, states, first, last, cutoff, not reasons
                )
            for k in range(window_start, window_end + 1):
                path = _metric(days, values, states, window_start, k, cutoff, not reasons)
                row["window_points"].append({
                    "x": k, "date": days[k].isoformat(),
                    "close": str(values[k]) if values[k] is not None else None,
                    "value": _ratio(values[window_start], values[k]),
                    "status": states[k], "normalized_status": path["status"],
                    "eligible": path["eligible"], "session": True,
                })
            row["baseline_extra_date"] = days[-6].isoformat()
            row["baseline_extra_close"] = str(values[-6]) if values[-6] is not None else None
            row["eligible"] = not reasons and all(
                window["complete"] for window in row["windows"].values()
            )
            for k in range(-5, 6):
                path = _metric(days, values, states, min(-1, k), max(-1, k), cutoff, not reasons)
                daily = _metric(days, values, states, k - 1, k, cutoff, not reasons)
                point = {
                    "x": k,
                    "date": days[k].isoformat(),
                    "close": str(values[k]) if values[k] is not None else None,
                    "value": _ratio(values[-1], values[k]),
                    "daily_return": daily["value"],
                    "status": states[k],
                    "normalized_status": path["status"],
                    "daily_status": daily["status"],
                    "eligible": path["eligible"],
                    "daily_eligible": daily["eligible"],
                    "session": True,
                }
                row["points"].append(point)
                cells.append({"key": key, "year": year, "category": category, **point})
            if any(not window["complete"] for window in row["windows"].values()):
                row["exclusion_reasons"] = [*reasons, "incomplete_full_window"]
        else:
            row["exclusion_reasons"] = [*reasons, "missing_or_conflicting_event_date"]
        rows.append(row)
        series.append(
            {
                "key": key,
                "label": event["event_name"],
                "year": year,
                "group": group,
                "category": category,
                "points": row["points"],
            }
        )
        window_series.append({
            "key": key, "label": event["event_name"], "year": year,
            "group": group, "category": category,
            "baseline_date": row["windows"].get(selected_window, {}).get("start_date"),
            "points": row["window_points"],
        })
        if row["exclusion_reasons"]:
            exclusions.append({"key": key, "reasons": row["exclusion_reasons"]})
    summarize_overlaps(rows)
    summarize_overlaps(coverage_rows)
    qualify_common_quarters(
        rows, metadata.get("common_years", False),
        quarters=metadata.get("requested_fiscal_quarters") or (1, 2, 3, 4),
    )
    for row in rows:
        row["eligible"] = row["eligible"] and all(w["eligible"] for w in row["windows"].values())
        row["selected_window_eligible"] = row["windows"].get(selected_window, {}).get("eligible", False)
        for point in row["window_points"]:
            point["eligible"] = point["eligible"] and row["selected_window_eligible"]
    category_summary = []
    for category in sorted({row["category"] for row in rows}):
        selected = [row for row in rows if row["category"] == category]
        historical = [row for row in selected if row["group"] == "historical"]
        windows = {}
        for name in WINDOWS:
            windows[name] = _statistics(
                [
                    row["windows"][name]["value"]
                    for row in historical
                    if row["windows"].get(name, {}).get("eligible")
                ]
            )
        paths = []
        for k in range(-5, 6):
            points = [point for row in historical for point in row["points"] if point["x"] == k]
            paths.append(
                {
                    "x": k,
                    **_statistics([point["value"] for point in points if point["eligible"]]),
                    "daily": _statistics(
                        [point["daily_return"] for point in points if point["daily_eligible"]]
                    ),
                }
            )
        category_summary.append(
            {
                "category": category,
                "event_count": len(selected),
                "year_count": len({row["year"] for row in selected if row["year"] is not None}),
                "historical_event_count": len(historical),
                "effective_n": sum(row["eligible"] for row in historical),
                "date_only_n": sum(
                    row["eligible"] and (not row["time_verified"] or row["event_time"] is None)
                    for row in historical
                ),
                "windows": windows,
                "path": paths,
                "window_path": [
                    {"x": k, **_statistics([
                        point["value"] for row in historical for point in row["window_points"]
                        if point["x"] == k and point["eligible"]
                    ])} for k in range(window_start, window_end + 1)
                ],
            }
        )
    return add_distributions({
        "kind": "event_dates",
        "metadata": {
            **metadata,
            "cutoff_date": cutoff.isoformat(),
            "current_year": current_year,
            "current_fiscal_year": current_fiscal_year,
            "calendar": calendar,
            "calendar_version": calendar_version(),
            "calculation_version": CALCULATION_VERSION,
            "representation_version": REPRESENTATION_VERSION,
            "price_basis": "split_only",
            "alignment": "event_date",
            "baseline": "D-1 close",
            "before": 5,
            "after": 5,
            "weighting": "equal_event_within_category",
            "date_window": selected_window,
            "window_start": window_start,
            "window_end": window_end,
        },
        "rows": rows,
        "coverage_rows": coverage_rows,
        "series": series,
        "window_series": window_series,
        "cells": cells,
        "category_summary": category_summary,
        "effective_n": sum(row["eligible"] for row in rows),
        "exclusions": exclusions,
    }, benchmark)
