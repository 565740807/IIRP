"""Sector performance from the 24-hour daily cache and saved stock quotes.

Sectors and their ETFs come from config/sector-map.toml. Returns are price
returns on the cache's split-only adjusted closes. Reads never call a provider:
the page asks once for the ETFs' daily prices (one fetch per ETF covering this
year and the configured complete past years, reused by the monthly and
interval analyses) and for quotes through the shared stock quote request.

Periods, all ending at the close of the last completed session L that the
cache holds:

- 1w, 1m, 3m: the start is the close of the latest session on or before the
  calendar date 7 days, 1 month or 3 months before L (a month back from
  March 31 is the last day of February).
- ytd: the start is the close of the previous year's last session.
- today: during the regular session, the latest regular-session quote against
  the previous session's close ("intraday", with the quote time); otherwise
  the close of L against the close of the session before it.

A start before an ETF's first daily bar has no value; history is never filled in.
"""

import calendar as gregorian
import tomllib
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache

from sqlalchemy import func, select

from iirp.analysis.calendar import ET, as_et, last_completed_session, previous_session, sessions
from iirp.config import ROOT
from iirp.models import PriceCache, PriceCacheBar, Security, now

PERIODS = ("today", "1w", "1m", "3m", "ytd")
CHART_RANGES = {"3m": 3, "6m": 6, "1y": 12, "ytd": None}
INTERVALS = ("day", "week", "month")
COVERAGE = ("complete", "partial", "reference", "pending")
CHANGE_STEP = Decimal("0.000001")


@lru_cache
def sector_map() -> dict:
    """config/sector-map.toml, checked once."""
    with (ROOT / "config/sector-map.toml").open("rb") as source:
        config = tomllib.load(source)
    sectors = config["sector"]
    ids = [item["id"] for item in sectors]
    etfs = [item["etf"] for item in sectors]
    if len(set(ids)) != len(ids) or len(set(etfs)) != len(etfs):
        raise ValueError("config/sector-map.toml: sector ids and ETFs must be unique")
    for item in sectors:
        for group in item.get("group", []):
            if group.get("coverage") not in COVERAGE:
                raise ValueError(f"config/sector-map.toml: unknown coverage for {group.get('id')}")
            if (group["coverage"] == "pending") != (group.get("primary") is None):
                raise ValueError(f"config/sector-map.toml: {group['id']} needs a primary ETF "
                                 "unless its coverage is pending")
    floors = config["heatmap"]["spread_floor_percent"]
    if set(floors) != set(PERIODS):
        raise ValueError("config/sector-map.toml: one heatmap spread floor per period")
    return config


def sector_etfs() -> list[str]:
    return [item["etf"] for item in sector_map()["sector"]]


def history_start(today: date) -> date:
    """January 1 of the first complete past year the sector page fetches."""
    return date(today.year - sector_map()["history"]["years"], 1, 1)


def months_back(day: date, months: int) -> date:
    """The same calendar day ``months`` earlier, or that month's last day."""
    year, month = divmod(day.year * 12 + day.month - 1 - months, 12)
    month += 1
    return date(year, month, min(day.day, gregorian.monthrange(year, month)[1]))


def period_anchor(end: date, period: str) -> date:
    """Calendar date whose session (that day or the latest before it) starts the period."""
    if period == "1w":
        return end - timedelta(days=7)
    if period == "1m":
        return months_back(end, 1)
    if period == "3m":
        return months_back(end, 3)
    if period == "ytd":
        return date(end.year - 1, 12, 31)
    raise ValueError(period)


def _decimal(value) -> Decimal | None:
    try:
        result = Decimal(str(value)) if value is not None else None
    except ArithmeticError:
        return None
    return result if result is not None and result.is_finite() and result > 0 else None


def change_text(first, last) -> str | None:
    first, last = _decimal(first), _decimal(last)
    if first is None or last is None:
        return None
    return format((last / first - 1).quantize(CHANGE_STEP), "f")


def _closes(bars: list[dict]) -> dict[date, Decimal]:
    return {date.fromisoformat(bar["date"]): close for bar in bars
            if bar.get("status") == "VALID" and (close := _decimal(bar.get("close"))) is not None}


def _change(closes, first_date, start: date, end: date, fetched_from: date | None) -> dict:
    result = {"value": None, "start_date": start.isoformat(), "end_date": end.isoformat()}
    if first_date is None:
        return {**result, "status": "no_prices"}
    if start < first_date:
        # Only a fetch that asked for earlier dates shows the ETF had not started trading.
        listed = fetched_from is not None and fetched_from <= start
        return {**result, "status": "before_listing" if listed else "missing"}
    if start not in closes or end not in closes:
        return {**result, "status": "missing"}
    return {**result, "value": change_text(closes[start], closes[end]), "status": "available"}


def closing_periods(closes: dict[date, Decimal], first_date: date | None, last: date,
                    fetched_from: date | None = None, calendar: str = "XNYS") -> dict:
    """1w, 1m, 3m and ytd ending at the close of ``last``."""
    result = {}
    for period in PERIODS[1:]:
        start = previous_session(period_anchor(last, period), calendar, inclusive=True)
        result[period] = _change(closes, first_date, start, last, fetched_from)
    return result


def _quote_day(value) -> date | None:
    return as_et(value).date() if value else None


def today_change(closes: dict[date, Decimal], first_date: date | None, quote: dict | None,
                 at: datetime, calendar: str = "XNYS") -> dict:
    """Today's change: intraday during the regular session, else the last two closes."""
    from iirp.market.stock_quotes import completed_close, stock_session

    quote = quote or {}
    period, _ = stock_session(at)
    completed = last_completed_session(at, calendar)
    prior = previous_session(completed, calendar)
    day = at.astimezone(ET).date()
    price, quoted = _decimal(quote.get("regular_value")), quote.get("regular_time")
    if period == "regular" and price is not None and _quote_day(quoted) == day:
        previous = closes.get(completed)
        if previous is None and _quote_day(quote.get("fetched_at")) == day:
            previous = _decimal(quote.get("previous_close"))
        return {"value": change_text(previous, price), "start_date": completed.isoformat(),
                "end_date": day.isoformat(), "mode": "intraday", "as_of": quoted,
                "delayed": quote.get("status") == "STALE",
                "status": "available" if previous is not None else "missing"}
    result = {"start_date": prior.isoformat(), "end_date": completed.isoformat(),
              "mode": "close", "as_of": None, "delayed": False}
    if completed in closes and prior in closes:
        return {**result, "value": change_text(closes[prior], closes[completed]), "status": "available"}
    # Before the cache holds the latest close: a quote saved on that day after
    # its close pairs the close with Yahoo's previous close for that same day.
    if (price is not None and _quote_day(quoted) == completed
            and _quote_day(quote.get("fetched_at")) == completed and completed_close(quote, at)):
        previous = closes.get(prior) or _decimal(quote.get("previous_close"))
        if previous is not None:
            return {**result, "value": change_text(previous, price), "status": "available"}
    if first_date is not None and prior < first_date:
        return {**result, "value": None, "status": "before_listing"}
    return {**result, "value": None, "status": "missing"}


def heatmap_weights(values: dict[str, str | None], period: str) -> dict[str, float | None]:
    """Tile weight per key: 0.2 + 0.8 × (r − r_min) / max(r_max − r_min, s)."""
    settings = sector_map()["heatmap"]
    floor = Decimal(str(settings["spread_floor_percent"][period])) / 100
    minimum = Decimal(str(settings["min_weight"]))
    known = {key: Decimal(value) for key, value in values.items() if value is not None}
    if not known:
        return {key: None for key in values}
    low, high = min(known.values()), max(known.values())
    spread = max(high - low, floor)
    return {key: float(round(minimum + (1 - minimum) * (known[key] - low) / spread, 6))
            if key in known else None for key in values}


def full_years(first_date: date | None, today: date, years: int, calendar: str = "XNYS") -> int:
    """Complete past years within the window that the ETF traded from their first session."""
    if first_date is None:
        return 0
    count = 0
    for year in range(today.year - years, today.year):
        opening = sessions(date(year, 1, 1), date(year, 1, 31), calendar)[0]
        count += first_date <= opening
    return count


def _caches(s, etfs: list[str], at: datetime):
    rows = s.execute(select(Security, PriceCache)
                     .join(PriceCache, PriceCache.security_id == Security.id)
                     .where(Security.symbol.in_(etfs), PriceCache.expires_at > at)).all()
    return {security.symbol: (security, cache) for security, cache in rows}


def _first_dates(s, cache_ids) -> dict[str, date]:
    if not cache_ids:
        return {}
    return dict(s.execute(select(PriceCacheBar.cache_id, func.min(PriceCacheBar.session_date))
                          .where(PriceCacheBar.cache_id.in_(cache_ids), PriceCacheBar.status == "VALID")
                          .group_by(PriceCacheBar.cache_id)).all())


def _bars(s, cache_ids, since: date, *, ohlc=False) -> dict[str, list[dict]]:
    columns = [PriceCacheBar.cache_id, PriceCacheBar.session_date, PriceCacheBar.close]
    if ohlc:
        columns += [PriceCacheBar.open, PriceCacheBar.high, PriceCacheBar.low]
    result = defaultdict(list)
    if not cache_ids:
        return result
    for row in s.execute(select(*columns).where(
            PriceCacheBar.cache_id.in_(cache_ids), PriceCacheBar.session_date >= since,
            PriceCacheBar.status == "VALID").order_by(PriceCacheBar.session_date)).mappings():
        bar = {"date": row["session_date"].isoformat(), "close": row["close"], "status": "VALID"}
        if ohlc:
            bar.update(open=row["open"], high=row["high"], low=row["low"])
        result[row["cache_id"]].append(bar)
    return result


def performance(s, *, at: datetime | None = None) -> dict:
    """Every sector's periods, the heatmap weights and what the page still needs."""
    from iirp.market.stock_quotes import stock_quote_views

    at = at or now()
    config = sector_map()
    etfs = sector_etfs()
    today = at.astimezone(ET).date()
    completed = last_completed_session(at)
    caches = _caches(s, etfs, at)
    cache_ids = [cache.id for _, cache in caches.values()]
    first_dates = _first_dates(s, cache_ids)
    # Only what the longest period needs: the previous year's last sessions and 3 months back.
    since = min(date(completed.year - 1, 12, 1), months_back(completed, 3) - timedelta(days=10))
    bars = _bars(s, cache_ids, since)
    quotes = {item["symbol"]: item for item in stock_quote_views(s, etfs)}
    items = []
    for sector in config["sector"]:
        etf = sector["etf"]
        security, cache = caches.get(etf, (None, None))
        closes = _closes(bars.get(cache.id, [])) if cache else {}
        first = first_dates.get(cache.id) if cache else None
        last = max((day for day in closes if day <= completed), default=None)
        periods = {"today": today_change(closes, first, quotes.get(etf), at)}
        if last is not None:
            periods.update(closing_periods(closes, first, last, cache.start_date))
        else:
            periods.update({period: {"value": None, "start_date": None, "end_date": None,
                                     "status": "no_prices"} for period in PERIODS[1:]})
        quote = quotes.get(etf) or {}
        items.append({
            "id": sector["id"], "etf": etf, "periods": periods,
            "first_date": first.isoformat() if first else None,
            "full_years": full_years(first, today, config["history"]["years"]),
            "quote_status": {"LIVE": "live", "CLOSED": "closed", "STALE": "delayed"}.get(
                quote.get("status"), "missing"),
            "quote_time": quote.get("source_time"),
            "price_fetched_at": cache.fetched_at.isoformat() if cache else None,
            "price_expires_at": cache.expires_at.isoformat() if cache else None,
            "prices_current": bool(cache and cache.complete_through >= completed
                                   and cache.start_date <= history_start(today)),
        })
    weights = {period: heatmap_weights({item["id"]: item["periods"][period]["value"]
                                        for item in items}, period) for period in PERIODS}
    for item in items:
        item["weights"] = {period: weights[period][item["id"]] for period in PERIODS}
    # The home strip's order: today's change, largest first, unknown last.
    items.sort(key=lambda item: (item["periods"]["today"]["value"] is None,
                                 -Decimal(item["periods"]["today"]["value"] or 0)))
    from iirp.market.stock_quotes import stock_session

    return {"as_of": at.isoformat(), "market_period": stock_session(at)[0],
            "last_completed_session": completed.isoformat(),
            "history_start": history_start(today).isoformat(),
            "history_years": config["history"]["years"],
            "items": items, "quote_symbols": etfs,
            "prices_pending": [item["etf"] for item in items if not item["prices_current"]]}


def chart_start(end: date, chart_range: str) -> date:
    months = CHART_RANGES[chart_range]
    return date(end.year, 1, 1) if months is None else months_back(end, months) + timedelta(days=1)


def aggregate(bars: list[dict], interval: str) -> list[dict]:
    """Daily bars as day, week (Monday–Friday) or calendar month candles.

    A week or month opens at its first session's open, closes at its last
    session's close, and spans the highest high and lowest low in between.
    """
    if interval not in INTERVALS:
        raise ValueError(interval)
    groups: dict[tuple, list[dict]] = {}
    for bar in bars:
        day = date.fromisoformat(bar["date"])
        key = ((day,) if interval == "day" else day.isocalendar()[:2] if interval == "week"
               else (day.year, day.month))
        groups.setdefault(key, []).append(bar)
    candles = []
    for members in groups.values():
        highs = [value for bar in members if (value := _decimal(bar.get("high"))) is not None]
        lows = [value for bar in members if (value := _decimal(bar.get("low"))) is not None]
        opening, closing = _decimal(members[0].get("open")), _decimal(members[-1].get("close"))
        if opening is None or closing is None or not highs or not lows:
            continue
        candles.append({"date": members[0]["date"], "end_date": members[-1]["date"],
                        "open": str(opening), "high": str(max(highs)), "low": str(min(lows)),
                        "close": str(closing), "sessions": len(members)})
    return candles


def candles(s, *, chart_range: str = "3m", interval: str = "day", at: datetime | None = None) -> dict:
    """K-line data for every sector over one shared range, aggregated in the backend."""
    at = at or now()
    etfs = sector_etfs()
    completed = last_completed_session(at)
    start = chart_start(completed, chart_range)
    # Weeks and months start at their own first session, so the first candle is whole.
    since = (start - timedelta(days=start.weekday()) if interval == "week"
             else start.replace(day=1) if interval == "month" else start)
    caches = _caches(s, etfs, at)
    cache_ids = [cache.id for _, cache in caches.values()]
    bars = _bars(s, cache_ids, since, ohlc=True)
    first_dates = _first_dates(s, cache_ids)
    items = []
    for sector in sector_map()["sector"]:
        etf = sector["etf"]
        _, cache = caches.get(etf, (None, None))
        rows = [bar for bar in bars.get(cache.id, []) if bar["date"] <= completed.isoformat()] if cache else []
        first = first_dates.get(cache.id) if cache else None
        items.append({"id": sector["id"], "etf": etf, "candles": aggregate(rows, interval),
                      "first_date": first.isoformat() if first else None,
                      "price_fetched_at": cache.fetched_at.isoformat() if cache else None})
    return {"range": chart_range, "interval": interval, "start_date": since.isoformat(),
            "end_date": completed.isoformat(), "items": items}


def request_prices(s, *, foreground: bool = False, at: datetime | None = None) -> dict:
    """Plan one daily-price fetch for the ETFs whose cache misses the sector range.

    Each fetch covers this year and the configured complete past years (the
    cache adds its month of buffer), so monthly and interval analyses opened
    from the sector page reuse it. Asking again the same day, until the next
    close, returns the same batch instead of adding work.
    """
    from iirp.jobs.batches import _create
    from iirp.market.cache import covers
    from iirp.market.yahoo import digest
    from iirp.messages import msg

    at = at or now()
    today = at.astimezone(ET).date()
    start = history_start(today)
    caches = _caches(s, sector_etfs(), at)
    due = [etf for etf in sector_etfs()
           if not covers(caches.get(etf, (None, None))[1], start, today)]
    if not due:
        return {"requested": [], "batch_ids": []}
    params = {"kind": "market_history", "tickers": due, "start_date": start.isoformat(),
              "end_date": today.isoformat(), "purpose": "sectors"}
    params["request_id"] = "sector-prices-" + digest(
        [params, last_completed_session(at).isoformat(), foreground])[:40]
    batch, reused = (_create(s, params) if foreground
                     else _create(s, params, trigger="automatic", policy_key="market"))
    if not reused:
        batch.title = msg("batch.title.sector_prices", count=len(due))
    return {"requested": due, "batch_ids": [batch.id]}
