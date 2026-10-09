"""One durable, coalesced Yahoo quote request for the stocks visible on screen."""

import re
import time
from datetime import timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from iirp.analysis.calendar import ET, last_completed_session, session_bounds, sessions
from iirp.config import refresh
from iirp.market.quotes import _next_extended_start, _timestamp
from iirp.market.yahoo import digest, number
from iirp.messages import UserError, msg
from iirp.models import ACTIVE, Job, MarketQuote, Subscription, now

KIND = "market_stock_quotes"
_crumb = None


def symbols_for(values, *, limit=200):
    """Canonical Yahoo equity symbols; input cannot change the provider URL."""
    symbols = sorted({str(value).strip().upper().replace(".", "-") for value in values})
    if (limit is not None and len(symbols) > limit) or any(not re.fullmatch(r"[A-Z0-9][A-Z0-9^=-]{0,31}", v) for v in symbols):
        raise UserError("market.stock_symbols_invalid")
    return symbols


def stock_session(stamp):
    """Calendar-aware D22 stock cadence, including exchange early closes."""
    local = stamp.astimezone(ET)
    day = local.date()
    if sessions(day, day):
        opening, closing = session_bounds(day)
        if opening <= local < closing:
            return "regular", stamp + timedelta(seconds=refresh()["quotes"]["session_seconds"])
        pre = opening.replace(hour=4, minute=0)
        post = closing.replace(hour=20, minute=0)
        if pre <= local < opening or closing <= local < post:
            boundary = opening if local < opening else post
            due = stamp + timedelta(seconds=refresh()["quotes"]["extended_seconds"])
            return "pre" if local < opening else "post", min(due, boundary)
    return "closed", _next_extended_start(stamp).astimezone(timezone.utc)


def completed_close(data, stamp, *, closing=None):
    """A saved regular-session quote from the latest close needs no closed refresh."""
    quoted = _timestamp(data.get("regular_time"))
    if quoted is None or number(data.get("regular_value")) is None:
        return False
    if closing is None:
        _, closing = session_bounds(last_completed_session(stamp))
    # Yahoo timestamps the final regular trade a few seconds before the bell.
    return closing - timedelta(minutes=1) <= quoted <= closing + timedelta(minutes=5)


def request_stock_quotes(s, symbols):
    """Return durable job IDs; share fresh cache and active symbol coverage across tabs.

    The caller owns the transaction. Network never runs here. Explicit paused or
    cancelled task intent is never undone by another page opening.
    """
    symbols = symbols_for(symbols)
    if not symbols:
        return []
    s.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": int(digest(KIND)[:15], 16)})
    stamp = now()
    period, _ = stock_session(stamp)
    closing = session_bounds(last_completed_session(stamp))[1] if period == "closed" else None
    cached = {row.symbol: row for row in s.scalars(select(MarketQuote).where(MarketQuote.symbol.in_(symbols)))}
    needed = {symbol for symbol in symbols if symbol not in cached
              or (not (period == "closed" and completed_close(cached[symbol].data, stamp, closing=closing))
                  and (not _timestamp(cached[symbol].data.get("next_refresh_at"))
                       or _timestamp(cached[symbol].data.get("next_refresh_at")) <= stamp))}
    identifiers = []
    for job in s.scalars(select(Job).where(Job.kind == KIND, Job.status.in_(ACTIVE))):
        covered = needed.intersection(job.target.get("symbols", []))
        if covered:
            identifiers.append(job.id)
            needed.difference_update(covered)
    if not needed:
        return identifiers
    target = {"symbols": sorted(needed)}
    job = Job(kind=KIND, title=msg("job.title.default", kind=KIND, symbol=",".join(target["symbols"])),
              target=target, idempotency_key=digest([KIND, target]), priority=-20)
    s.add(job)
    s.flush()
    s.add(Subscription(job_id=job.id, source="manual"))
    return [*identifiers, job.id]


def fetch_stock_quotes(target):
    """Fetch all symbols using one quote endpoint call, inside the Yahoo worker lane.

    Yahoo's public endpoint uses an ephemeral cookie and crumb, as does yfinance.
    They stay in memory and are never included in the saved response or diagnostics.
    Authentication does not retry failed HTTP calls; the existing worker budget and
    retry policy handle failures. The warm worker process reuses its session.
    """
    from iirp.market.http import timed_session

    global _crumb
    symbols = symbols_for(target["symbols"])
    if not symbols:
        return {"symbols": [], "quotes": [], "fetched_at": now().isoformat()}
    timing = timed_session()
    timing.reset()
    started = time.perf_counter()
    if _crumb is None:
        cookie = timing.session.get("https://fc.yahoo.com", timeout=12)
        # This endpoint normally returns 404 while setting the authentication cookie.
        if cookie.status_code not in (200, 404):
            cookie.raise_for_status()
        auth = timing.session.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=12)
        auth.raise_for_status()
        if not auth.text or auth.text.startswith(("{", "<")):
            raise UserError("market.no_quote")
        _crumb = auth.text
    response = timing.session.get("https://query1.finance.yahoo.com/v7/finance/quote",
                                  params={"symbols": ",".join(symbols), "crumb": _crumb}, timeout=12)
    if response.status_code in (401, 403):
        _crumb = None
    response.raise_for_status()
    body = response.json().get("quoteResponse") or {}
    if body.get("error") is not None or not isinstance(body.get("result"), list):
        raise UserError("market.no_quote")
    keys = ("symbol", "regularMarketPrice", "regularMarketTime", "preMarketPrice", "preMarketTime",
            "postMarketPrice", "postMarketTime", "marketState", "currency", "exchangeTimezoneName")
    return {"provider": "yahoo", "symbols": symbols,
            "quotes": [{key: row.get(key) for key in keys} for row in body["result"]],
            "fetched_at": now().isoformat(),
            "timing": {**timing.snapshot(), "adapter_seconds": time.perf_counter() - started,
                       "adapter_calls": 1}}


def stock_quote(symbol, raw, fetched):
    period, due = stock_session(fetched)
    price, quoted = number(raw.get("regularMarketPrice")), _timestamp(raw.get("regularMarketTime"))
    kind = "regular"
    if period in ("pre", "post"):
        extended = "preMarket" if period == "pre" else "postMarket"
        extra_price, extra_time = number(raw.get(extended + "Price")), _timestamp(raw.get(extended + "Time"))
        if extra_price is not None and extra_time and quoted and extra_time > quoted:
            price, quoted, kind = extra_price, extra_time, period
    valid = price is not None and price > 0 and quoted is not None and quoted <= fetched + timedelta(minutes=5)
    stale = valid and period != "closed" and (fetched - quoted).total_seconds() > 20 * 60
    return {"symbol": symbol, "value": str(price) if valid else None,
            "as_of": quoted.astimezone(ET).date().isoformat() if valid else None,
            "source_time": quoted.isoformat() if valid else None, "fetched_at": fetched.isoformat(),
            "next_refresh_at": due.isoformat(), "quote_kind": "stock_snapshot",
            "price_session": kind, "market_period": period,
            "regular_value": str(number(raw.get("regularMarketPrice"))) if number(raw.get("regularMarketPrice")) is not None else None,
            "regular_time": _timestamp(raw.get("regularMarketTime")).isoformat() if _timestamp(raw.get("regularMarketTime")) else None,
            "status": "NOT_FETCHED" if not valid else "CLOSED" if period == "closed" else "STALE" if stale else "LIVE",
            "reason": msg("market.no_quote") if not valid else msg("quote.delay.stale") if stale else None,
            "currency": raw.get("currency") or "USD"}


def persist_stock_quotes(s, current, response):
    """Publish only inside handlers' existing fenced commit; ignore older responses."""
    fetched = _timestamp(response.get("fetched_at"))
    if fetched is None:
        raise UserError("market.no_quote")
    requested = symbols_for(current.target["symbols"])
    quotes = {row.get("symbol"): row for row in response.get("quotes", [])}
    missing = []
    for symbol in requested:
        data = stock_quote(symbol, quotes.get(symbol, {}), fetched)
        if data["value"] is None:
            missing.append(symbol)
            existing = s.get(MarketQuote, symbol)
            if existing and existing.data.get("value") is not None:
                # An incomplete batch response must retain the previous factual price.
                data = {**existing.data, "status": "STALE", "reason": msg("market.no_quote"),
                        "next_refresh_at": data["next_refresh_at"]}
        statement = insert(MarketQuote).values(symbol=symbol, data=data, fetched_at=fetched)
        s.execute(statement.on_conflict_do_update(index_elements=[MarketQuote.symbol],
            set_={"data": data, "fetched_at": fetched}, where=MarketQuote.fetched_at <= fetched))
    return {"quoted": len(requested) - len(missing), "missing_symbols": missing}


def stock_quote_views(s, symbols):
    """Read-only short-cache projection, with freshness evaluated at read time."""
    symbols = symbols_for(symbols, limit=None)
    stamp = now()
    period, _ = stock_session(stamp)
    expected_closing = session_bounds(last_completed_session(stamp))[1] if period == "closed" else None
    rows = {row.symbol: row for row in s.scalars(select(MarketQuote).where(MarketQuote.symbol.in_(symbols)))}
    result = []
    for symbol in symbols:
        row = rows.get(symbol)
        if row is None:
            result.append({"symbol": symbol, "value": None, "as_of": None, "source_time": None,
                           "fetched_at": None, "next_refresh_at": None, "status": "NOT_FETCHED",
                           "reason": msg("market.no_quote")})
            continue
        data = dict(row.data)
        closing = _timestamp(data.get("regular_time"))
        if period == "closed" and closing and number(data.get("regular_value")) is not None:
            data.update(value=data["regular_value"], source_time=closing.isoformat(),
                        as_of=closing.astimezone(ET).date().isoformat(), status="CLOSED",
                        price_session="regular", market_period="closed")
        due = _timestamp(data.get("next_refresh_at"))
        if due and due <= stamp and data.get("value") is not None and not (
            period == "closed" and completed_close(data, stamp, closing=expected_closing)
        ):
            data["status"], data["reason"] = "STALE", msg("quote.delay.stale")
        result.append(data)
    return result
