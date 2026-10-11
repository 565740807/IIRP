"""Price reaction around dated events, centered on the reaction day R.

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
daily prices; nothing is fetched here. Ratios are decimal strings, never
percents; the frontend only formats them.

Events may carry AI-compiled background (``context``) and expectations
(``earnings``); they are copied to the rows unverified. Each row with
context also gets daily candles over the union of R-1-n..R+n and the
context dates. Displays derived from them are recomputed from the frozen
rows by ``project_result``:

- surprise = (actual - estimate) / |estimate| for EPS and revenue; the EPS
  surprise groups earnings into beat / in line (|surprise| below the
  configured threshold) / miss, events without both figures into no_data;
  guidance groups them into raised / maintained / lowered / none / no_data.
- an event is "major macro" when a context item marked major, of category
  macro, policy or geopolitical, is dated within the configured number of
  sessions before or after R; such events can be left out of every statistic.
"""

import tomllib
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from functools import lru_cache

from iirp.analysis.calendar import next_session, session_window, sessions
from iirp.analysis.distributions import paired_ratio, statistics, wilson_interval
from iirp.analysis.sector_benchmarks import public_sector
from iirp.config import ROOT

CALCULATION_VERSION = "event-windows-v4-context"
WINDOWS = ("before", "reaction", "gap", "after")
SURPRISE_GROUPS = ("beat", "inline", "miss", "no_data")
GUIDANCE_GROUPS = ("raised", "maintained", "lowered", "none", "no_data")


@lru_cache
def group_settings():
    """[event_groups] of config/analysis-defaults.toml."""
    with (ROOT / "config/analysis-defaults.toml").open("rb") as source:
        values = tomllib.load(source)["event_groups"]
    return {"inline_threshold": Decimal(str(values["inline_threshold"])),
            "macro_sessions": int(values["macro_sessions"]),
            "macro_categories": tuple(values["macro_categories"])}


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


def _window(prices, start, end, cutoff, end_field="close", benchmark=None, sector=None):
    """Change from C(start) to C(end), or to O(end) for the opening gap."""
    output = {"value": None, "start_date": start.isoformat(), "end_date": end.isoformat(),
              "status": "pending", "benchmark": None, "excess": None, "sector": None, "sector_excess": None}
    if end > cutoff:
        return output
    output["value"] = _change(_price(prices, start), _price(prices, end, end_field))
    output["status"] = "ok" if output["value"] is not None else "missing_price"
    if benchmark is not None:
        output["benchmark"] = _change(_price(benchmark, start), _price(benchmark, end, end_field))
        output["excess"] = _minus(output["value"], output["benchmark"])
    if sector is not None:
        output["sector"] = _change(_price(sector, start), _price(sector, end, end_field))
        output["sector_excess"] = _minus(output["value"], output["sector"])
    return output


def window_stats(windows):
    """N, median, mean, quartiles, the up share with its Wilson 95% interval,
    the median absolute move and, when paired, how often the benchmark was beaten."""
    values = [w["value"] for w in windows if w["value"] is not None]
    stats = statistics(values)
    with localcontext() as context:
        context.prec = 34
        absolute = statistics([abs(Decimal(v)) for v in values])
    paired = [w for w in windows if w.get("excess") is not None]
    excess = statistics([w["excess"] for w in paired])
    sector_paired = [w for w in windows if w.get("sector_excess") is not None]
    sector_excess = statistics([w["sector_excess"] for w in sector_paired])
    output = {
        **{key: stats.get(key) for key in ("n", "median", "mean", "q25", "q75", "min", "max", "up", "flat",
                                                         "up_ratio", "whisker_low", "whisker_high", "outliers")},
        "abs_median": absolute.get("median"),
        "up_low": None, "up_high": None, "coin_flip": None,
        "paired_n": excess["n"], "beat": excess["up"],
        "median_excess": excess.get("median"), "mean_excess": excess.get("mean"),
        "beat_ratio": paired_ratio(excess["up"], excess["n"]),
        "benchmark_box": statistics([w["benchmark"] for w in windows]),
        "benchmark_median": statistics([w["benchmark"] for w in paired]).get("median"),
        "benchmark_excess_box": excess, "sector_excess_box": sector_excess,
        "sector_box": statistics([w.get("sector") for w in windows]),
        "sector_paired_n": sector_excess["n"], "sector_beat": sector_excess["up"],
        "sector_beat_ratio": paired_ratio(sector_excess["up"], sector_excess["n"]),
        "sector_median": statistics([w["sector"] for w in sector_paired]).get("median"),
        "sector_median_excess": sector_excess.get("median"), "sector_mean_excess": sector_excess.get("mean"),
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
                       "sector_median": statistics([p.get("sector") for p in points]).get("median"),
                       "benchmark_median": statistics([p["benchmark"] for p in points
                                                       if p["benchmark"] is not None]).get("median")})
    return output


def analyze_events(events, bars, *, n, cutoff, calendar="XNYS", kind="custom", benchmark=None, sector=None):
    """One ticker's result: rows per event, overall summary, the average path and,
    for earnings, Q1-Q4. ``benchmark`` is ``{"symbol", "status", "bars"}`` or None."""
    if n < 1:
        raise ValueError("n must be positive")
    prices = _bars(bars)
    paired = _bars(benchmark.get("bars")) if benchmark and benchmark.get("status") == "available" else None
    sector_paired = _bars(sector.get("bars")) if sector and sector.get("status") == "available" else None
    rows = []
    for event in sorted(events, key=lambda e: e["date"]):
        day = date.fromisoformat(event["date"])
        reaction = reaction_day(day, event["session"], calendar)
        days = session_window(reaction, n + 1, n, calendar)  # R-1-n .. R+n
        base, before_start, after_end = days[n], days[0], days[-1]
        windows = {
            "before": _window(prices, before_start, base, cutoff, benchmark=paired, sector=sector_paired),
            "reaction": _window(prices, base, reaction, cutoff, benchmark=paired, sector=sector_paired),
            "gap": _window(prices, base, reaction, cutoff, end_field="open", benchmark=paired, sector=sector_paired),
            "after": _window(prices, reaction, after_end, cutoff, benchmark=paired, sector=sector_paired),
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
        sector_base = _price(sector_paired, base) if sector_paired else None
        for offset, session_day in zip(range(-n, n + 1), days[1:], strict=True):
            bar = prices.get(session_day) if session_day <= cutoff else None
            candles.append({"offset": offset, "date": session_day.isoformat(),
                            **{field: str(bar[field]) if bar and bar[field] is not None else None
                               for field in ("open", "high", "low", "close")}})
            formed = session_day <= cutoff
            path.append({"offset": offset, "date": session_day.isoformat(),
                         "value": _change(base_close, _price(prices, session_day)) if formed else None,
                         "benchmark": _change(benchmark_base, _price(paired, session_day))
                         if formed and paired else None,
                         "sector": _change(sector_base, _price(sector_paired, session_day))
                         if formed and sector_paired else None})
        context = event.get("context") or []
        macro = session_window(reaction, group_settings()["macro_sessions"],
                               group_settings()["macro_sessions"], calendar)
        context_candles = None
        if context:
            first = min(days[0], date.fromisoformat(min(item["date"] for item in context)))
            last = max(days[-1], date.fromisoformat(max(item["date"] for item in context)))
            span = sessions(first, last, calendar)
            position = span.index(reaction)
            context_candles = [
                {"offset": index - position, "date": day.isoformat(),
                 **{field: str(bar[field]) if bar and bar[field] is not None else None
                    for field in ("open", "high", "low", "close")}}
                for index, day in enumerate(span)
                for bar in (prices.get(day) if day <= cutoff else None,)]
        notes = []
        if event["session"] == "unknown":
            notes.append("session_unknown")
        if event["session"] != "after_close" and reaction != day:
            notes.append("market_closed_on_date")
        rows.append({
            **{key: event.get(key) for key in ("ticker", "date", "session", "name", "note",
                                               "fiscal_year", "fiscal_quarter", "earnings")},
            "uncertain": bool(event.get("uncertain")),
            "context": context,
            "context_candles": context_candles,
            "macro_window": {"start": macro[0].isoformat(), "end": macro[-1].isoformat(),
                             "sessions": group_settings()["macro_sessions"]},
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
        "sector": public_sector(sector), "sector_comparison_included": True,
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


def _surprise(actual, estimate):
    if actual is None or estimate is None:
        return None
    estimate = Decimal(str(estimate))
    if estimate == 0:
        return None
    with localcontext() as context:
        context.prec = 34
        return str((Decimal(str(actual)) - estimate) / abs(estimate))


def expectations(row):
    """EPS and revenue surprise and the groups of one row; empty figures give no_data."""
    figures = row.get("earnings") or {}
    eps = _surprise(figures.get("eps_actual"), figures.get("eps_estimate"))
    threshold = group_settings()["inline_threshold"]
    group = "no_data" if eps is None else "inline" if abs(Decimal(eps)) < threshold else (
        "beat" if Decimal(eps) > 0 else "miss")
    return {"eps_surprise": eps,
            "revenue_surprise": _surprise(figures.get("revenue_actual"), figures.get("revenue_estimate")),
            "surprise_group": group, "guidance_group": figures.get("guidance") or "no_data"}


def macro_events(row):
    """Major macro, policy or geopolitical context dated within the row's window around R."""
    window = row.get("macro_window")
    if not window:
        return []
    categories = group_settings()["macro_categories"]
    return [item for item in row.get("context") or []
            if item.get("major") and item.get("category") in categories
            and window["start"] <= item["date"] <= window["end"]]


def _groups(rows, key, order):
    return [{"key": name, "event_count": len(selected), "summary": _summary(selected)}
            for name in order if (selected := [row for row in rows if row["expectations"][key] == name])]


def project_result(data, *, quarter=None, recent_years=None, session=None, direction=None, exclude_macro=False):
    """Recompute displays from frozen event rows, without reading or fetching prices.

    Recent years are calendar years ending in the analysis cutoff year.
    Missing and flat reactions belong to neither the up nor down subset.
    With ``exclude_macro`` events with major macro context near R are left
    out of every statistic and counted in ``macro_excluded``.
    """
    first_year = date.fromisoformat(data["cutoff_date"]).year - recent_years + 1 if recent_years else None
    rows = [dict(row) for row in data["rows"]
            if (quarter is None or row.get("fiscal_quarter") == quarter)
            and (first_year is None or int(row["date"][:4]) >= first_year)
            and (session is None or row["session"] == session)
            and (direction is None or (row["windows"]["reaction"]["value"] is not None
                 and (Decimal(row["windows"]["reaction"]["value"]) > 0 if direction == "up"
                      else Decimal(row["windows"]["reaction"]["value"]) < 0)))]
    for row in rows:
        row["id"] = "|".join([row["ticker"], row["date"], row["name"]])
        row["ranks"] = {}
        # Results made before background existed lack these fields.
        for key, empty in (("uncertain", False), ("earnings", None), ("context", []),
                           ("context_candles", None), ("macro_window", None)):
            row.setdefault(key, empty)
        row["expectations"] = expectations(row)
        row["macro_events"] = macro_events(row)
    excluded = sum(bool(row["macro_events"]) for row in rows) if exclude_macro else 0
    if exclude_macro:
        rows = [row for row in rows if not row["macro_events"]]
    for window in WINDOWS:
        valid = [row for row in rows if row["windows"][window]["value"] is not None]
        for order in ("asc", "desc"):
            ranked = sorted(valid, key=lambda row: Decimal(row["windows"][window]["value"]), reverse=order == "desc")
            for rank, row in enumerate(ranked, 1):
                row["ranks"][f"{window}_{order}"] = rank
    output = {**data, "rows": rows, "event_count": len(rows), "summary": _summary(rows),
              "path": _path_stats(rows, data["n"]), "macro_excluded": excluded,
              "macro_sessions": group_settings()["macro_sessions"]}
    if data["event_kind"] == "earnings":
        output["groups"] = {"inline_threshold": str(group_settings()["inline_threshold"]),
                            "surprise": _groups(rows, "surprise_group", SURPRISE_GROUPS),
                            "guidance": _groups(rows, "guidance_group", GUIDANCE_GROUPS)}
        output["quarters"] = [{"fiscal_quarter": q, "event_count": len(selected), "summary": _summary(selected)}
                              for q in (1, 2, 3, 4)
                              if (selected := [row for row in rows if row.get("fiscal_quarter") == q])]
        for window in WINDOWS:
            for metric in ("median", "up_ratio", "median_excess", "sector_median_excess"):
                for order in ("asc", "desc"):
                    valid = [q for q in output["quarters"] if q["summary"][window][metric] is not None]
                    for rank, q in enumerate(sorted(valid, key=lambda q: Decimal(q["summary"][window][metric]),
                                                    reverse=order == "desc"), 1):
                        q.setdefault("ranks", {})[f"{window}_{metric}_{order}"] = rank
    return output
