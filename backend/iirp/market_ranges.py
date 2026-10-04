"""Bounded trading-session gaps shared by stock and benchmark planning."""

from datetime import date, timedelta

# More than the normal eight complete years plus current year. This is a request
# budget, never a cap on the research scope. Wider explicit ranges keep going.
MAX_REQUEST_DAYS = 366 * 10
MIN_SPLIT_DAYS = 31


def merge_missing_ranges(missing, calendar="XNYS"):
    from iirp.analytics.calendar import sessions

    ordered = sorted(set(missing))
    if not ordered:
        return []
    positions = {day: i for i, day in enumerate(sessions(ordered[0], ordered[-1], calendar))}
    groups = []
    for day in ordered:
        if (
            not groups
            or positions[day] != positions[groups[-1][-1]] + 1
            or (day - groups[-1][0]).days >= MAX_REQUEST_DAYS
        ):
            groups.append([])
        groups[-1].append(day)
    return [(group[0], group[-1]) for group in groups]


def split_failed_range(target):
    """Bisect only a failed large request, retaining exact, non-overlapping dates."""
    start, end = (date.fromisoformat(target[key]) for key in ("start_date", "end_date"))
    if (end - start).days < MIN_SPLIT_DAYS:
        return []
    middle = start + (end - start) // 2
    return [
        {**target, "start_date": str(start), "end_date": str(middle)},
        {**target, "start_date": str(middle + timedelta(days=1)), "end_date": str(end)},
    ]


def range_failure(response):
    """Throttling/auth/unavailable symbols must back off, never multiply calls."""
    if response.get("source_wait") or response.get("status_code") in (401, 403, 429):
        return False
    return response.get("error_type") in {
        "TimeoutError",
        "ReadTimeout",
        "ConnectTimeout",
        "Timeout",
        "RequestTooLarge",
    } or response.get("status_code") in (413, 414)
