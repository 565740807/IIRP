"""Current Yahoo snapshots, kept separate from completed research price datasets."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from iirp.analysis.calendar import last_completed_session, previous_session
from iirp.market.yahoo import MARKETS, number
from iirp.messages import msg


def _timestamp(value):
    """Source timestamps must identify an instant; never assume a local timezone."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, timezone.utc)
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, datetime) and value.tzinfo is not None:
            return value.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _periods(meta):
    result = []
    raw = meta.get("currentTradingPeriod")
    if not isinstance(raw, dict):
        return result
    # The snapshot fields are regularMarketPrice/Time. Yahoo may also return
    # generic pre/post windows for indexes without a matching extended quote.
    for name in ("regular",):
        period = raw.get(name)
        if not isinstance(period, dict):
            continue
        start, end = _timestamp(period.get("start")), _timestamp(period.get("end"))
        if start and end and end > start:
            result.append({"period": name, "start": start, "end": end,
                           "timezone": meta.get("exchangeTimezoneName") or period.get("timezone")})
    return result


def _daily_records(response):
    records = {}
    for row in response.get("records", []):
        try:
            day = datetime.strptime(row.get("date", ""), "%Y-%m-%d").date().isoformat()
        except (TypeError, ValueError):
            continue
        if number(row.get("close")) is not None:
            records[day] = row
    return [records[day] for day in sorted(records)]


def quote_from_snapshot(symbol, response):
    """Pair price and time from one response, with explicit baseline and session evidence.

    chartPreviousClose is the close BEFORE the requested chart range, not necessarily
    yesterday's close. We therefore use the preceding returned daily session, with its
    date disclosed, and never manufacture a change when that baseline is unavailable.
    """
    records = _daily_records(response)
    meta = response.get("metadata") or {}
    fetched = _timestamp(response.get("fetched_at"))
    quoted = _timestamp(meta.get("regularMarketTime"))
    price = number(meta.get("regularMarketPrice"))
    # A future stamp cannot make an old cached response appear up to date.
    paired = (price is not None and quoted is not None and fetched is not None
              and quoted <= fetched + timedelta(minutes=5))
    if not paired and not records:
        return None
    known_zone = bool(meta.get("exchangeTimezoneName"))
    try:
        exchange_zone = ZoneInfo(meta.get("exchangeTimezoneName") or "UTC")
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        exchange_zone, known_zone = timezone.utc, False

    periods = _periods(meta)
    active = next((p for p in periods if fetched and p["start"] <= fetched < p["end"]), None)
    upcoming = sorted((p for p in periods if fetched and p["start"] > fetched), key=lambda p: p["start"])
    # The metadata belongs to this instrument, including futures and VIX. Do not
    # apply an equity-hours/calendar shortcut to either its trading day or refresh.
    market_open = bool(active) if periods and fetched else None
    selected = active or (upcoming[0] if upcoming else max(periods, key=lambda p: p["end"], default=None))
    session = ({**selected, "start": selected["start"].isoformat(),
                "end": selected["end"].isoformat(), "basis": msg("quote.session_basis")}
               if selected else None)
    next_refresh = None
    if fetched:
        if active:
            next_refresh = fetched + timedelta(seconds=60)
        elif upcoming:
            next_refresh = upcoming[0]["start"]
        else:
            next_refresh = fetched + timedelta(minutes=15 if market_open is False else 5)

    source_time = quoted.isoformat() if paired else None
    if paired:
        quote_period = next((p for p in periods if p["start"] <= quoted <= p["end"]), None)
        # Overnight futures sessions carry the trading date of their end, while a
        # stock close/unknown session retains the source quote's exchange-local day.
        trading_day = (quote_period["end"].astimezone(exchange_zone).date()
                       if quote_period else quoted.astimezone(exchange_zone).date()).isoformat()
        prior = (next((r for r in reversed(records) if r["date"] < trading_day), None)
                 if known_zone else None)
        value = price
        age = max(0, (fetched - quoted).total_seconds())
        stale = market_open is True and (age > 20 * 60 or quoted < active["start"])
        status = "STALE" if stale else "CLOSED" if market_open is False else "DELAYED"
        delay = msg("quote.delay.stale" if stale else
                    "quote.delay.closed" if market_open is False else "quote.delay.delayed")
        quote_kind = "provider_snapshot"
    else:
        latest = records[-1]
        trading_day, value = latest["date"], number(latest["close"])
        prior = records[-2] if len(records) > 1 else None
        status, delay, quote_kind = "DAILY", msg("quote.delay.daily"), "daily_fallback"
    last_available_date = prior["date"] if prior else None
    expected_prior = previous_session(date.fromisoformat(trading_day)).isoformat() if symbol in {"^GSPC", "^IXIC", "^DJI", "^VIX"} else None
    gap = expected_prior is not None and last_available_date != expected_prior
    previous = number(prior["close"]) if prior and not gap else None
    baseline = (
        msg("quote.baseline.gap", expected=expected_prior,
            last=last_available_date or msg("common.none")) if gap
        else msg("quote.baseline.previous_futures" if symbol == "GC=F" else "quote.baseline.previous")
        if previous is not None else msg("quote.baseline.missing")
    )
    return {
        "symbol": symbol,
        "name": MARKETS.get(symbol, symbol),
        "value": float(value),
        "change_percent": float((value / previous - 1) * 100) if previous else None,
        "change": float(value - previous) if previous is not None else None,
        "previous_close": str(previous) if previous is not None else None,
        "baseline_date": prior["date"] if prior and not gap else None,
        "baseline_gap_date": expected_prior if gap else None,
        "last_available_close_date": last_available_date,
        "last_completed_session": last_completed_session(fetched).isoformat() if fetched and expected_prior else None,
        "as_of": trading_day,
        "source_time": source_time,
        "status": status,
        "delay": delay,
        "baseline": baseline,
        "quote_kind": quote_kind,
        "market_open": market_open,
        "session": session,
        "next_refresh_at": next_refresh.isoformat() if next_refresh else None,
        "unit": msg("quote.unit.usd_per_ounce" if symbol == "GC=F" else "quote.unit.index_points"),
        "instrument": msg("quote.instrument.gold_futures" if symbol == "GC=F" else "quote.instrument.index"),
        "exchange_timezone": meta.get("exchangeTimezoneName"),
        "records": records,
        "fetched_at": response.get("fetched_at"),
    }
