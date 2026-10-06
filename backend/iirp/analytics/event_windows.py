"""Price reaction around dated events, centered on the reaction day R (D23).

- ``before_open`` / ``during``: R is the event date, or the next session when
  the market is closed that day; ``unknown`` is treated the same and flagged.
- ``after_close``: R is the first session after the event date.

With C = close, O = open and n sessions:
  before   = C(R-1) / C(R-1-n) - 1
  reaction = C(R) / C(R-1) - 1        gap = O(R) / C(R-1) - 1
  after    = C(R+n) / C(R) - 1
The three windows do not overlap. Bars are the cached split-only daily prices
(D14); nothing is fetched here. Ratios are decimal strings, never percents.
"""

from datetime import date
from decimal import Decimal, InvalidOperation, localcontext

from iirp.analytics.calendar import next_session, session_window
from iirp.analytics.distributions import statistics

CALCULATION_VERSION = "event-windows-v1"
WINDOWS = ("before", "reaction", "gap", "after")


def reaction_day(event_date, session, calendar="XNYS"):
    """R for an event date and session; see the module docstring."""
    day = date.fromisoformat(event_date) if isinstance(event_date, str) else event_date
    return next_session(day, calendar, inclusive=session != "after_close")


def sessions_needed(events, n, calendar="XNYS"):
    """First and last session any window needs: R-1-n of the earliest, R+n of the latest."""
    days = [reaction_day(e["date"], e["session"], calendar) for e in events]
    if not days:
        return None
    return (session_window(min(days), n + 1, 0, calendar)[0],
            session_window(max(days), 0, n, calendar)[-1])


def _number(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None
    return number if number.is_finite() and number > 0 else None


def _bars(bars):
    output = {}
    for bar in bars:
        if bar.get("status") != "VALID":
            continue
        day = date.fromisoformat(bar["date"]) if isinstance(bar["date"], str) else bar["date"]
        output[day] = {field: _number(bar.get(field)) for field in ("open", "high", "low", "close")}
    return output


def _change(first, last):
    if first is None or last is None:
        return None
    with localcontext() as context:
        context.prec = 34
        return str(last / first - 1)


def _window(prices, start, end, cutoff, end_field="close"):
    """Change from C(start) to C(end), or to O(end) for the opening gap."""
    if end > cutoff:
        return {"value": None, "start_date": start.isoformat(), "end_date": end.isoformat(),
                "status": "pending"}
    value = _change((prices.get(start) or {}).get("close"), (prices.get(end) or {}).get(end_field))
    return {"value": value, "start_date": start.isoformat(), "end_date": end.isoformat(),
            "status": "ok" if value is not None else "missing_price"}


def summarize(values):
    """N, median, quartiles and the share of positive values."""
    stats = statistics(values)
    with localcontext() as context:
        context.prec = 34
        stats["up_ratio"] = str(Decimal(stats["up"]) / stats["n"]) if stats["n"] else None
    return stats


def _summary(rows):
    return {name: summarize([row["windows"][name]["value"] for row in rows
                             if row["windows"][name]["value"] is not None])
            for name in WINDOWS}


def analyze_events(events, bars, *, n, cutoff, calendar="XNYS", kind="custom"):
    """One ticker's result: rows per event, overall summary and, for earnings, Q1-Q4."""
    if n < 1:
        raise ValueError("n must be positive")
    prices = _bars(bars)
    rows = []
    for event in sorted(events, key=lambda e: e["date"]):
        day = date.fromisoformat(event["date"])
        reaction = reaction_day(day, event["session"], calendar)
        days = session_window(reaction, n + 1, n, calendar)  # R-1-n .. R+n
        base, before_start, after_end = days[n], days[0], days[-1]
        windows = {
            "before": _window(prices, before_start, base, cutoff),
            "reaction": _window(prices, base, reaction, cutoff),
            "gap": _window(prices, base, reaction, cutoff, end_field="open"),
            "after": _window(prices, reaction, after_end, cutoff),
        }
        candles = []
        for offset, session_day in zip(range(-n, n + 1), days[1:], strict=True):
            bar = prices.get(session_day) if session_day <= cutoff else None
            candles.append({"offset": offset, "date": session_day.isoformat(),
                            **{field: str(bar[field]) if bar and bar[field] is not None else None
                               for field in ("open", "high", "low", "close")}})
        notes = []
        if event["session"] == "unknown":
            notes.append("session_unknown")
        if event["session"] != "after_close" and reaction != day:
            notes.append("market_closed_on_date")
        rows.append({
            **{key: event.get(key) for key in ("ticker", "date", "session", "name", "note",
                                               "fiscal_year", "fiscal_quarter")},
            "reaction_date": reaction.isoformat(),
            "baseline_date": base.isoformat(),
            "windows": windows,
            "candles": candles,
            "notes": notes,
        })
    result = {
        "kind": "event_windows",
        "event_kind": kind,
        "n": n,
        "cutoff_date": cutoff.isoformat(),
        "calendar": calendar,
        "calculation_version": CALCULATION_VERSION,
        "event_count": len(rows),
        "summary": _summary(rows),
        "rows": rows,
    }
    if kind == "earnings":
        result["quarters"] = [
            {"fiscal_quarter": quarter, "event_count": len(selected), "summary": _summary(selected)}
            for quarter in (1, 2, 3, 4)
            if (selected := [row for row in rows if row.get("fiscal_quarter") == quarter])
        ]
    return result
