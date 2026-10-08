"""Price reaction around dated events, centered on the reaction day R (D23).

- ``before_open`` / ``during``: R is the event date, or the next session when
  the market is closed that day; ``unknown`` is treated the same and flagged.
- ``after_close``: R is the first session after the event date.

With C = close, O = open and n sessions:
  before   = C(R-1) / C(R-1-n) - 1
  reaction = C(R) / C(R-1) - 1        gap = O(R) / C(R-1) - 1
  after    = C(R+n) / C(R) - 1
The three windows do not overlap: before ends at the close of R-1 where the
reaction day starts, and after starts at the close of R where it ends. The gap
is the opening part of the reaction day, shown on its own.

Each event also has its reaction-day candle relative to C(R-1) (open = gap,
close = reaction, high and low), and its path C(t)/C(R-1) - 1 for t = R-n..R+n.
A benchmark, when given, gets the same windows over the same dates; the excess
is the stock's window minus the benchmark's. Bars are the cached split-only
daily prices (D14); nothing is fetched here. Ratios are decimal strings, never
percents; the frontend only formats them.
"""

from datetime import date
from decimal import Decimal, InvalidOperation, localcontext

from iirp.analysis.calendar import next_session, session_window
from iirp.analysis.distributions import statistics, wilson_interval

CALCULATION_VERSION = "event-windows-v2"
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
    for bar in bars or ():
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


def _minus(a, b):
    if a is None or b is None:
        return None
    with localcontext() as context:
        context.prec = 34
        return str(Decimal(a) - Decimal(b))


def _price(prices, day, field="close"):
    return (prices.get(day) or {}).get(field)


def _window(prices, start, end, cutoff, end_field="close", benchmark=None):
    """Change from C(start) to C(end), or to O(end) for the opening gap."""
    output = {"value": None, "start_date": start.isoformat(), "end_date": end.isoformat(),
              "status": "pending", "benchmark": None, "excess": None}
    if end > cutoff:
        return output
    output["value"] = _change(_price(prices, start), _price(prices, end, end_field))
    output["status"] = "ok" if output["value"] is not None else "missing_price"
    if benchmark is not None:
        output["benchmark"] = _change(_price(benchmark, start), _price(benchmark, end, end_field))
        output["excess"] = _minus(output["value"], output["benchmark"])
    return output


def window_stats(windows):
    """N, median, mean, quartiles, the up share with its Wilson 95% interval,
    the median absolute move and, when paired, how often the benchmark was beaten."""
    values = [w["value"] for w in windows if w["value"] is not None]
    stats = statistics(values)
    with localcontext() as context:
        context.prec = 34
        absolute = statistics([abs(Decimal(v)) for v in values])
    paired = [w for w in windows if w["excess"] is not None]
    excess = statistics([w["excess"] for w in paired])
    output = {
        **{key: stats.get(key) for key in ("n", "median", "mean", "q25", "q75", "min", "max", "up", "flat")},
        "abs_median": absolute.get("median"),
        "up_low": None, "up_high": None, "coin_flip": None,
        "paired_n": excess["n"], "beat": excess["up"],
        "median_excess": excess.get("median"), "mean_excess": excess.get("mean"),
        "benchmark_median": statistics([w["benchmark"] for w in paired]).get("median"),
    }
    if stats["n"]:
        low, high = wilson_interval(stats["up"], stats["n"])
        output.update(up_low=str(low), up_high=str(high), coin_flip=low <= Decimal("0.5") <= high)
    return output


def _summary(rows):
    return {name: window_stats([row["windows"][name] for row in rows]) for name in WINDOWS}


def _path_stats(rows, n):
    """Median and middle half of the paths at each offset, and the benchmark's median."""
    output = []
    for index, offset in enumerate(range(-n, n + 1)):
        points = [row["path"][index] for row in rows]
        stats = statistics([p["value"] for p in points if p["value"] is not None])
        output.append({"offset": offset, "n": stats["n"], "median": stats.get("median"),
                       "q25": stats.get("q25"), "q75": stats.get("q75"),
                       "benchmark_median": statistics([p["benchmark"] for p in points
                                                       if p["benchmark"] is not None]).get("median")})
    return output


def analyze_events(events, bars, *, n, cutoff, calendar="XNYS", kind="custom", benchmark=None):
    """One ticker's result: rows per event, overall summary, the average path and,
    for earnings, Q1-Q4. ``benchmark`` is ``{"symbol", "status", "bars"}`` or None."""
    if n < 1:
        raise ValueError("n must be positive")
    prices = _bars(bars)
    paired = _bars(benchmark.get("bars")) if benchmark and benchmark.get("status") == "available" else None
    rows = []
    for event in sorted(events, key=lambda e: e["date"]):
        day = date.fromisoformat(event["date"])
        reaction = reaction_day(day, event["session"], calendar)
        days = session_window(reaction, n + 1, n, calendar)  # R-1-n .. R+n
        base, before_start, after_end = days[n], days[0], days[-1]
        windows = {
            "before": _window(prices, before_start, base, cutoff, benchmark=paired),
            "reaction": _window(prices, base, reaction, cutoff, benchmark=paired),
            "gap": _window(prices, base, reaction, cutoff, end_field="open", benchmark=paired),
            "after": _window(prices, reaction, after_end, cutoff, benchmark=paired),
        }
        base_close = _price(prices, base)
        candle = None
        if reaction <= cutoff and base_close is not None:
            candle = {field: _change(base_close, _price(prices, reaction, field))
                      for field in ("open", "high", "low", "close")}
            if candle["close"] is None:
                candle = None
        candles, path = [], []
        benchmark_base = _price(paired, base) if paired else None
        for offset, session_day in zip(range(-n, n + 1), days[1:], strict=True):
            bar = prices.get(session_day) if session_day <= cutoff else None
            candles.append({"offset": offset, "date": session_day.isoformat(),
                            **{field: str(bar[field]) if bar and bar[field] is not None else None
                               for field in ("open", "high", "low", "close")}})
            formed = session_day <= cutoff
            path.append({"offset": offset, "date": session_day.isoformat(),
                         "value": _change(base_close, _price(prices, session_day)) if formed else None,
                         "benchmark": _change(benchmark_base, _price(paired, session_day))
                         if formed and paired else None})
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
            "reaction_candle": candle,
            "path": path,
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
        "benchmark": {"symbol": benchmark["symbol"], "status": benchmark.get("status", "unknown")}
        if benchmark else None,
        "event_count": len(rows),
        "summary": _summary(rows),
        "path": _path_stats(rows, n),
        "rows": rows,
    }
    if kind == "earnings":
        result["quarters"] = [
            {"fiscal_quarter": quarter, "event_count": len(selected), "summary": _summary(selected)}
            for quarter in (1, 2, 3, 4)
            if (selected := [row for row in rows if row.get("fiscal_quarter") == quarter])
        ]
    return result
