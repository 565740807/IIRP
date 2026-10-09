"""Exchange-session selection; no network calls and no weekday approximations.

Dates denote exchange-local session labels. Timestamp inputs without an offset
are explicitly interpreted as America/New_York (the SEC acceptance convention).
Callers handling another source must convert its timestamp before using it.
"""

from datetime import date, datetime, timedelta
from functools import lru_cache
from importlib.metadata import version
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

ET = ZoneInfo("America/New_York")


@lru_cache(maxsize=32)
def _calendar(name: str, first_year: int, last_year: int):
    # Explicit bounds avoid exchange_calendars' rolling default history window.
    return xcals.get_calendar(
        name,
        start=f"{first_year:04d}-01-01",
        end=f"{last_year:04d}-12-31",
    )


def _for_range(start: date, end: date, name: str):
    # Reuse one exchange calendar for nearby queries. The wider bounds change
    # only construction cost; sessions_in_range still applies the exact dates.
    first = max(1, start.year - 1)
    last = min(9999, end.year + 1)
    decade_start = max(1, first // 10 * 10)
    decade_end = min(9999, (last // 10 + 1) * 10 - 1)
    return _calendar(name, decade_start, decade_end)


def calendar_version() -> str:
    return f"exchange-calendars/{version('exchange-calendars')}"


def sessions(start: date, end: date, calendar: str = "XNYS") -> list[date]:
    """Inclusive range of actual exchange sessions, including early closes."""
    if start > end:
        return []
    cal = _for_range(start, end, calendar)
    return [stamp.date() for stamp in cal.sessions_in_range(start.isoformat(), end.isoformat())]


def session_bounds(day: date, calendar: str = "XNYS") -> tuple[datetime, datetime]:
    """Return timezone-aware actual open/close; reject non-session dates."""
    cal = _for_range(day, day, calendar)
    label = day.isoformat()
    return (
        cal.session_open(label).to_pydatetime().astimezone(ET),
        cal.session_close(label).to_pydatetime().astimezone(ET),
    )


def next_session(day: date, calendar: str = "XNYS", *, inclusive: bool = True) -> date:
    cursor = day if inclusive else day + timedelta(days=1)
    while True:
        candidates = sessions(cursor, cursor + timedelta(days=31), calendar)
        if candidates:
            return candidates[0]
        cursor += timedelta(days=32)


def previous_session(day: date, calendar: str = "XNYS", *, inclusive: bool = False) -> date:
    cursor = day if inclusive else day - timedelta(days=1)
    while True:
        candidates = sessions(cursor - timedelta(days=31), cursor, calendar)
        if candidates:
            return candidates[-1]
        cursor -= timedelta(days=32)


def session_window(anchor: date, before: int, after: int, calendar: str = "XNYS") -> list[date]:
    """Exactly ``before + 1 + after`` sessions around an actual session."""
    if before < 0 or after < 0:
        raise ValueError("Session counts must be nonnegative")
    if not sessions(anchor, anchor, calendar):
        raise ValueError(f"{anchor} is not a {calendar} session")
    span = max(31, (before + after + 1) * 2)
    while True:
        days = sessions(anchor - timedelta(days=span), anchor + timedelta(days=span), calendar)
        index = days.index(anchor)
        if index >= before and len(days) - index - 1 >= after:
            return days[index - before : index + after + 1]
        span *= 2


def as_et(value: str | datetime) -> datetime:
    stamp = (
        datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    )
    return stamp.replace(tzinfo=ET) if stamp.tzinfo is None else stamp.astimezone(ET)


def last_completed_session(as_of: datetime | None = None, calendar: str = "XNYS") -> date:
    """Use the actual exchange close, including holidays, DST and early close."""
    stamp = as_et(as_of) if as_of is not None else datetime.now(ET)
    day = stamp.date()
    if sessions(day, day, calendar) and stamp >= session_bounds(day, calendar)[1]:
        return day
    return previous_session(day, calendar)


def completed_through(today: date | None = None, calendar: str = "XNYS") -> date:
    """Explicit historical dates mean end-of-day; real today never assumes close.

    Future test dates are not a source of future facts: the clock still bounds
    the result. For deterministic clock tests use ``last_completed_session``.
    """
    now = datetime.now(ET)
    if today is not None and today < now.date():
        return previous_session(today, calendar, inclusive=True)
    return last_completed_session(now, calendar)


def reaction_session(
    announced_at: str | datetime | None,
    *,
    announced_date: str | date | None = None,
    time_precision: str = "exact",
    calendar: str = "XNYS",
) -> dict:
    """Resolve R and B, retaining date-only/intraday observations as ineligible."""
    date_only_input = isinstance(announced_at, str) and len(announced_at) == 10
    stamp = as_et(announced_at) if announced_at is not None and not date_only_input else None
    event_date = (
        date.fromisoformat(announced_date) if isinstance(announced_date, str) else announced_date
    )
    if date_only_input and event_date is None:
        event_date = date.fromisoformat(announced_at)
    date_conflict = stamp is not None and event_date is not None and stamp.date() != event_date
    event_date = stamp.date() if stamp is not None else event_date
    if event_date is None:
        return {
            "reaction_date": None,
            "baseline_date": None,
            "opening_attribution": False,
            "status": "missing_event_date",
        }
    is_session = bool(sessions(event_date, event_date, calendar))
    precise = time_precision in {"exact", "before_open", "after_close"}
    intraday = time_precision == "intraday"
    category_conflict = False
    if stamp is not None and is_session:
        opening, closing = session_bounds(event_date, calendar)
        category_conflict = (time_precision == "before_open" and stamp >= opening) or (
            time_precision == "after_close" and stamp < closing
        )
    reaction = next_session(event_date, calendar)
    if time_precision == "after_close" and is_session:
        reaction = next_session(event_date, calendar, inclusive=False)
    elif time_precision == "exact":
        if stamp is None:
            precise = False
        elif is_session:
            opening, closing = session_bounds(event_date, calendar)
            if stamp >= closing:
                reaction = next_session(event_date, calendar, inclusive=False)
            elif stamp >= opening:
                intraday = True
                precise = False
    if time_precision in {"date_only", "conflict", "intraday"}:
        precise = False
    if date_conflict or category_conflict:
        precise = False
    return {
        "reaction_date": reaction.isoformat(),
        "baseline_date": previous_session(reaction, calendar).isoformat(),
        "opening_attribution": precise,
        "status": "conflicting_event_time"
        if date_conflict or category_conflict
        else "intraday_observation"
        if intraday
        else ("available" if precise else "unconfirmed_event_time"),
    }


def next_regular_open_after(value: str | datetime, calendar: str = "XNYS") -> date:
    stamp = as_et(value)
    candidate = next_session(stamp.date(), calendar)
    if session_bounds(candidate, calendar)[0] <= stamp:
        candidate = next_session(candidate, calendar, inclusive=False)
    return candidate
