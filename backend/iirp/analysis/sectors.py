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

Industry groups: each group ranks only its primary ETF. Alternates are
reference rows and pending groups (no primary ETF) are listed last; neither
is ranked, weighted on the heatmap or charted. The group ETFs are fetched only
when the industry group level is opened, never for the sector level or the
home strip.
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


def check_sector_map(config: dict) -> dict:
    """Reject a sector map that would rank the wrong ETF or show one ETF twice.

    Each group has a known coverage; a pending group has no primary and every
    other group has one. A group equal to its sector (same_as_sector) is the
    sector's only group and uses the sector's ETF; apart from that no ETF
    appears twice across sectors, primaries and alternates.
    """
    problem = "config/sector-map.toml: "
    sectors = config["sector"]
    ids = [item["id"] for item in sectors]
    etfs = [item["etf"] for item in sectors]
    if len(set(ids)) != len(ids) or len(set(etfs)) != len(etfs):
        raise ValueError(problem + "sector ids and ETFs must be unique")
    seen = set(etfs)
    group_ids = set()
    for item in sectors:
        groups = item.get("group", [])
        for group in groups:
            name = group.get("id")
            if not name or name in group_ids:
                raise ValueError(problem + f"group id {name!r} is missing or repeated")
            group_ids.add(name)
            if group.get("coverage") not in COVERAGE:
                raise ValueError(problem + f"unknown coverage for {name}")
            primary = group.get("primary")
            if (group["coverage"] == "pending") != (primary is None):
                raise ValueError(problem + f"{name} needs a primary ETF unless its coverage is pending")
            alternates = group.get("alternates", [])
            if not isinstance(alternates, list) or not all(isinstance(etf, str) and etf for etf in alternates):
                raise ValueError(problem + f"{name}: alternates must be a list of ETFs")
            if group.get("same_as_sector"):
                if len(groups) != 1 or primary != item["etf"]:
                    raise ValueError(problem + f"{name} equals its sector only as the sector's "
                                     "single group with the sector's ETF")
                group_etfs = alternates
            else:
                group_etfs = ([primary] if primary else []) + alternates
            for etf in group_etfs:
                if etf in seen:
                    raise ValueError(problem + f"ETF {etf} appears more than once")
                seen.add(etf)
    floors = config["heatmap"]["spread_floor_percent"]
    if set(floors) != set(PERIODS):
        raise ValueError(problem + "one heatmap spread floor per period")
    return config


@lru_cache
def sector_map() -> dict:
    """config/sector-map.toml, checked once."""
    with (ROOT / "config/sector-map.toml").open("rb") as source:
        return check_sector_map(tomllib.load(source))


def sector_etfs() -> list[str]:
    return [item["etf"] for item in sector_map()["sector"]]


def sector_ids() -> list[str]:
    return [item["id"] for item in sector_map()["sector"]]


def industry_groups(sector: str | None = None) -> list[dict]:
    """Industry groups in config order, of one sector or of all of them."""
    return [{"id": group["id"], "sector": item["id"], "primary": group.get("primary"),
             "alternates": list(group.get("alternates", [])), "coverage": group["coverage"],
             "same_as_sector": bool(group.get("same_as_sector"))}
            for item in sector_map()["sector"] if sector in (None, item["id"])
            for group in item.get("group", [])]


def group_etfs(sector: str | None = None) -> list[str]:
    """Every ETF the industry group level shows: primaries, then reference rows."""
    groups = industry_groups(sector)
    etfs = [group["primary"] for group in groups if group["primary"]]
    etfs += [etf for group in groups for etf in group["alternates"]]
    return list(dict.fromkeys(etfs))


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


QUOTE_STATUS = {"LIVE": "live", "CLOSED": "closed", "STALE": "delayed"}


def _fetch_status(s, etfs: list[str], at: datetime) -> dict[str, dict]:
    """Today's daily-price request of each ETF without a current cache.

    Read from the request scopes of today's market_history batches; an ETF
    nobody has asked for today is "not_requested".
    """
    from iirp.models import Batch, RequestScope

    if not etfs:
        return {}
    day_start = datetime.combine(at.astimezone(ET).date(), datetime.min.time(), ET)
    rows = s.execute(select(RequestScope.symbol, RequestScope.status, RequestScope.wait_reason)
                     .join(Batch, Batch.id == RequestScope.batch_id)
                     .where(Batch.kind == "market_history", Batch.created_at >= day_start,
                            RequestScope.symbol.in_(etfs))
                     .order_by(Batch.created_at)).all()
    result = {}
    for symbol, status, reason in rows:
        if status in ("FAILED", "PARTIAL", "CANCELLED"):
            empty = reason is not None and "market.empty_response" in reason
            result[symbol] = {"status": "no_data" if empty else "failed", "reason": reason}
        elif status in ("READY", "SUCCEEDED"):
            result.setdefault(symbol, {"status": "not_requested", "reason": None})
        else:
            result[symbol] = {"status": "fetching", "reason": None}
    return result


def _etf_views(s, etfs: list[str], at: datetime) -> dict[str, dict]:
    """Every period, listing date, quote state and cache state of each ETF."""
    from iirp.market.stock_quotes import stock_quote_views

    today = at.astimezone(ET).date()
    completed = last_completed_session(at)
    years = sector_map()["history"]["years"]
    caches = _caches(s, etfs, at)
    cache_ids = [cache.id for _, cache in caches.values()]
    first_dates = _first_dates(s, cache_ids)
    # Only what the longest period needs: the previous year's last sessions and 3 months back.
    since = min(date(completed.year - 1, 12, 1), months_back(completed, 3) - timedelta(days=10))
    bars = _bars(s, cache_ids, since)
    quotes = {item["symbol"]: item for item in stock_quote_views(s, etfs)}
    views = {}
    for etf in etfs:
        _, cache = caches.get(etf, (None, None))
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
        current = bool(cache and cache.complete_through >= completed
                       and cache.start_date <= history_start(today))
        views[etf] = {
            "etf": etf, "periods": periods,
            "first_date": first.isoformat() if first else None,
            "full_years": full_years(first, today, years),
            "quote_status": QUOTE_STATUS.get(quote.get("status"), "missing"),
            "quote_time": quote.get("source_time"),
            "price_fetched_at": cache.fetched_at.isoformat() if cache else None,
            "price_expires_at": cache.expires_at.isoformat() if cache else None,
            "prices_current": current,
            "fetch": {"status": "ready" if current else "not_requested", "reason": None},
        }
        if cache and first is None:
            views[etf]["fetch"] = {"status": "no_data", "reason": None}
    waiting = [etf for etf, view in views.items() if view["fetch"]["status"] == "not_requested"]
    for etf, status in _fetch_status(s, waiting, at).items():
        views[etf]["fetch"] = status
    return views


def _header(at: datetime) -> dict:
    from iirp.market.stock_quotes import stock_session

    today = at.astimezone(ET).date()
    return {"as_of": at.isoformat(), "market_period": stock_session(at)[0],
            "last_completed_session": last_completed_session(at).isoformat(),
            "history_start": history_start(today).isoformat(),
            "history_years": sector_map()["history"]["years"]}


def _today_order(item: dict):
    value = item["periods"]["today"]["value"] if item.get("periods") else None
    return value is None, -Decimal(value or 0)


def performance(s, *, at: datetime | None = None) -> dict:
    """Every sector's periods, the heatmap weights and what the page still needs."""
    at = at or now()
    etfs = sector_etfs()
    views = _etf_views(s, etfs, at)
    items = [{"id": sector["id"], **views[sector["etf"]], "groups": len(sector.get("group", []))}
             for sector in sector_map()["sector"]]
    weights = {period: heatmap_weights({item["id"]: item["periods"][period]["value"]
                                        for item in items}, period) for period in PERIODS}
    for item in items:
        item["weights"] = {period: weights[period][item["id"]] for period in PERIODS}
    # The home strip's order: today's change, largest first, unknown last.
    items.sort(key=_today_order)
    return {**_header(at), "items": items, "sector_ids": sector_ids(), "quote_symbols": etfs,
            "group_etfs": group_etfs(),
            "prices_pending": [item["etf"] for item in items if not item["prices_current"]]}


def ranks(values: dict[str, str | None]) -> dict[str, int | None]:
    """1 for the largest change; keys without a value have no rank."""
    known = sorted(((Decimal(value), key) for key, value in values.items() if value is not None),
                   key=lambda pair: -pair[0])
    order = {key: index + 1 for index, (_, key) in enumerate(known)}
    return {key: order.get(key) for key in values}


def group_performance(s, *, sector: str | None = None, at: datetime | None = None) -> dict:
    """Industry groups of one sector, or all 25, ranked by their primary ETF only.

    Heatmap weights and ranks are computed among the primaries shown. Each
    group carries its reference rows (alternates) with the same periods, but
    they are never ranked. Pending groups come last with no ETF.
    """
    if sector is not None and sector not in sector_ids():
        raise ValueError(sector)
    at = at or now()
    groups = industry_groups(sector)
    etfs = group_etfs(sector)
    views = _etf_views(s, etfs, at)
    items = []
    for group in groups:
        primary = views.get(group["primary"]) if group["primary"] else None
        items.append({
            "id": group["id"], "sector": group["sector"], "coverage": group["coverage"],
            "same_as_sector": group["same_as_sector"],
            **(primary or {"etf": None, "periods": None}),
            "alternates": [views[etf] for etf in group["alternates"]],
        })
    ranked = [item for item in items if item["periods"] is not None]
    weights = {period: heatmap_weights({item["id"]: item["periods"][period]["value"]
                                        for item in ranked}, period) for period in PERIODS}
    order = {period: ranks({item["id"]: item["periods"][period]["value"] for item in ranked})
             for period in PERIODS}
    for item in items:
        ranked_item = item["periods"] is not None
        item["weights"] = ({period: weights[period][item["id"]] for period in PERIODS}
                           if ranked_item else None)
        item["ranks"] = ({period: order[period][item["id"]] for period in PERIODS}
                         if ranked_item else None)
    items.sort(key=lambda item: (item["periods"] is None, *_today_order(item)))
    return {**_header(at), "sector": sector, "items": items, "quote_symbols": etfs,
            "prices_pending": [etf for etf in etfs if not views[etf]["prices_current"]]}


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


def chart_rows(level: str = "sector", sector: str | None = None) -> list[dict]:
    """(id, sector, etf) of every chart: the sectors, or the groups with a primary ETF."""
    if level == "sector":
        return [{"id": item["id"], "sector": item["id"], "etf": item["etf"]}
                for item in sector_map()["sector"]]
    if sector is not None and sector not in sector_ids():
        raise ValueError(sector)
    return [{"id": group["id"], "sector": group["sector"], "etf": group["primary"]}
            for group in industry_groups(sector) if group["primary"]]


def candles(s, *, chart_range: str = "3m", interval: str = "day", level: str = "sector",
            sector: str | None = None, at: datetime | None = None) -> dict:
    """K-line data for the sectors or industry groups over one shared range."""
    at = at or now()
    rows = chart_rows(level, sector)
    etfs = [row["etf"] for row in rows]
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
    for row in rows:
        _, cache = caches.get(row["etf"], (None, None))
        daily = [bar for bar in bars.get(cache.id, []) if bar["date"] <= completed.isoformat()] if cache else []
        first = first_dates.get(cache.id) if cache else None
        items.append({**row, "candles": aggregate(daily, interval),
                      "first_date": first.isoformat() if first else None,
                      "price_fetched_at": cache.fetched_at.isoformat() if cache else None})
    return {"range": chart_range, "interval": interval, "level": level, "sector": sector,
            "start_date": since.isoformat(), "end_date": completed.isoformat(), "items": items}


def request_prices(s, *, foreground: bool = False, level: str = "sector", sector: str | None = None,
                   at: datetime | None = None) -> dict:
    """Plan one daily-price fetch for the ETFs whose cache misses the sector range.

    The sector level asks for the 11 sector ETFs only; the industry group
    level for the groups' primaries and reference rows (of one sector, or of
    all of them). Each fetch covers this year and the configured complete past
    years (the cache adds its month of buffer), so monthly and interval
    analyses opened from the sector page reuse it. Asking again the same day,
    until the next close, returns the same batch instead of adding work.
    """
    from iirp.jobs.batches import _create
    from iirp.market.cache import covers
    from iirp.market.yahoo import digest
    from iirp.messages import msg

    at = at or now()
    today = at.astimezone(ET).date()
    start = history_start(today)
    if level == "sector":
        etfs = sector_etfs()
    elif sector is None or sector in sector_ids():
        etfs = group_etfs(sector)
    else:
        raise ValueError(sector)
    caches = _caches(s, etfs, at)
    due = [etf for etf in etfs if not covers(caches.get(etf, (None, None))[1], start, today)]
    if not due:
        return {"requested": [], "batch_ids": []}
    params = {"kind": "market_history", "tickers": due, "start_date": start.isoformat(),
              "end_date": today.isoformat(),
              "purpose": "sectors" if level == "sector" else "sector_groups"}
    params["request_id"] = "sector-prices-" + digest(
        [params, last_completed_session(at).isoformat(), foreground])[:40]
    batch, reused = (_create(s, params) if foreground
                     else _create(s, params, trigger="automatic", policy_key="market"))
    if not reused:
        batch.title = msg("batch.title.sector_prices" if level == "sector"
                          else "batch.title.sector_group_prices", count=len(due))
    return {"requested": due, "batch_ids": [batch.id]}
