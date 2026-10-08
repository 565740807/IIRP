"""Local market reads: price coverage, range previews, saved quotes and one symbol's detail."""

from datetime import date, datetime, timedelta

from sqlalchemy import select

from iirp.config import refresh
from iirp.db import session
from iirp.jobs.batches import ET, resolve_defaults
from iirp.market.cache import coverage_for, price_bars
from iirp.messages import NotFoundError, UserError, msg
from iirp.models import (
    ACTIVE,
    Batch,
    Job,
    MarketQuote,
    RequestScope,
    Security,
    now,
)


def _overdue_seconds(quote, fetched):
    """A quote is late after a multiple of the interval it was scheduled with
    (1 minute in session, 5 minutes in extended hours), never sooner than the floor."""
    cadence = refresh()["quotes"]
    floor = cadence["overdue_seconds"]
    due = quote.get("next_refresh_at")
    if not due:
        return floor
    interval = (datetime.fromisoformat(due) - fetched).total_seconds()
    return max(floor, cadence["overdue_factor"] * interval) if interval > 0 else floor


def _quote_freshness(quote, failure, current):
    """(freshness, reason message) of a saved quote at ``current``."""
    session = quote.get("session") or {}
    start, end = (datetime.fromisoformat(session[key]) if session.get(key) else None for key in ("start", "end"))
    fetched = datetime.fromisoformat(quote["fetched_at"])
    if failure is not None and failure.updated_at > fetched:
        return "delayed", failure.error or msg("home.quote.refresh_failed")
    if start and end and start <= current < end:
        if quote.get("status") == "STALE":
            return "delayed", msg("quote.delay.stale")
        overdue = _overdue_seconds(quote, fetched)
        if (current - fetched).total_seconds() > overdue:
            return "delayed", msg("home.quote.overdue", minutes=round(overdue / 60))
        return "live", None
    if end and current >= end and fetched < end:
        return "delayed", msg("home.quote.close_pending")
    if start and end:
        return "closed", None
    return ("delayed", msg("quote.delay.daily")) if quote.get("status") == "DAILY" else ("live", None)


def quote_views(s, names, current):
    """Saved quotes with their freshness at ``current``, in the order of ``names``."""
    quotes = {q.symbol: q for q in s.scalars(select(MarketQuote).where(MarketQuote.symbol.in_(list(names))))}
    # The newest ordinary quote job per symbol: a failure after the last fetch explains a delay.
    latest = {}
    for job in s.scalars(
        select(Job).where(Job.kind == "market_quote", Job.created_at > current - timedelta(days=1))
        .order_by(Job.created_at.desc()).limit(50)
    ):
        if not job.target.get("history"):
            latest.setdefault(job.target.get("symbol"), job)
    market = []
    for symbol, name in names.items():
        row, job = quotes.get(symbol), latest.get(symbol)
        failure = job if job is not None and job.status in ("FAILED", "RETRY_WAIT") else None
        if row is None:
            market.append({"symbol": symbol, "name": name, "status": "NOT_FETCHED", "freshness": "missing",
                           "reason": failure.error if failure else msg("home.quote_not_fetched")})
            continue
        quote = {key: value for key, value in row.data.items() if key not in ("history", "records")}
        quote.update(name=name, fetched_at=row.fetched_at.isoformat())
        quote["freshness"], quote["reason"] = _quote_freshness(quote, failure, current)
        market.append(quote)
    return market


def get_coverage(ticker="", security_id="", start_date="", end_date=""):
    with session() as s:
        from iirp.analysis.history_range import price_range
        if bool(start_date) != bool(end_date):
            raise UserError("common.dates_together")
        target = price_range(resolve_defaults(s, {"start_date": start_date, "end_date": end_date}), now())
        query = select(Security)
        if ticker:
            query = query.where(Security.symbol == ticker.upper())
        if security_id:
            query = query.where(Security.id == security_id)
        items = []
        for sec in s.scalars(query.order_by(Security.symbol).limit(100)):
            start = date.fromisoformat(target["target_start_date"])
            end = min(date.fromisoformat(target["target_end_date"]), date.fromisoformat(target["completed_through"]))
            items.append(
                {
                    "id": sec.id,
                    "security_id": sec.id,
                    "symbol": sec.symbol,
                    "name": sec.name,
                    "coverage": coverage_for(s, sec, start, end),
                    "price_range": target,
                }
            )
        return {"items": items, "data": {}}


def preview_price_range(historical_years=None, start_date=None, end_date=None, analysis=None):
    """Local planning only: no job, subscription, identity, or result mutation."""
    from iirp.analysis.calendar import last_completed_session
    from iirp.analysis.history_range import price_range
    from iirp.analysis.research import _interval_rules, plan_scope
    with session() as s:
        params = resolve_defaults(s, {"historical_years": historical_years,
                                      "start_date": start_date, "end_date": end_date})
        stamp = now()
        bounds = None
        if analysis:
            effective = {k: v for k, v in analysis.items() if v is not None}
            if effective["kind"] == "monthly":
                effective.setdefault("current_year", stamp.astimezone(ET).year)
            else:
                effective["current_year"] = _interval_rules(effective, stamp.astimezone(ET).date())[3]
            params["historical_years"] = effective["historical_years"]
            params["analysis_params"] = effective
            bounds = plan_scope(effective, today=last_completed_session(as_of=stamp))
        return price_range(params, stamp, collection=bounds)


def _history_batch(s, symbol):
    """The newest six-month request for this symbol's detail page."""
    return s.scalar(select(Batch).where(Batch.kind == "market_quotes", Batch.params["history"].astext == "true",
                                        Batch.params["tickers"].contains([symbol]))
                    .order_by(Batch.created_at.desc()).limit(1))


def _bars(records):
    return [{key: row.get(key) for key in ("date", "open", "high", "low", "close")} for row in records]


def market_detail(symbol):
    """One home-strip symbol: its saved quote and the last six months of daily bars.

    The bars come from the 24-hour six-month fetch kept with the quote, else
    from a cached price range, else from the quote's last 40 days. Nothing is
    fetched here; ``history.status`` tells the page whether to ask for them.
    """
    from iirp.market.quote_publication import HISTORY_DAYS, fresh_history
    from iirp.market.yahoo import MARKETS

    if symbol not in MARKETS:
        raise NotFoundError("market.symbol_unknown", symbol=symbol)
    current = now()
    start = (current.astimezone(ET) - timedelta(days=HISTORY_DAYS)).date().isoformat()
    with session() as s:
        quote = quote_views(s, {symbol: MARKETS[symbol]}, current)[0]
        row = s.get(MarketQuote, symbol)
        saved = row.data if row else {}
        history = fresh_history(saved, current)
        chart = {"source": None, "fetched_at": None, "expires_at": None}
        if history:
            bars = _bars(history["records"])
            chart.update(source="history", fetched_at=history["fetched_at"], expires_at=history["expires_at"])
        else:
            security = s.scalar(select(Security).where(Security.symbol == symbol).order_by(Security.id).limit(1))
            cached, cache = price_bars(s, security.id) if security else ([], None)
            cached = [bar for bar in cached if bar["date"] >= start and bar.get("status") == "VALID"]
            if cache and cached and cached[0]["date"] <= (date.fromisoformat(start) + timedelta(days=7)).isoformat():
                bars = _bars(cached)
                chart.update(source="cache", fetched_at=cache.fetched_at.isoformat(),
                             expires_at=cache.expires_at.isoformat())
            else:
                bars = _bars(saved.get("records", []))
                chart.update(source="quote" if bars else None,
                             fetched_at=row.fetched_at.isoformat() if row and bars else None)
        bars = [bar for bar in bars if bar["date"] >= start]
        batch = _history_batch(s, symbol)
        if history or chart["source"] == "cache":
            status, reason = "fresh", None
        elif batch is not None and batch.status in ACTIVE:
            status, reason = "fetching", None
        elif batch is not None and batch.created_at.astimezone(ET).date() == current.astimezone(ET).date() \
                and batch.status in ("FAILED", "PARTIAL"):
            reason = s.scalar(select(RequestScope.wait_reason).where(RequestScope.batch_id == batch.id).limit(1))
            status, reason = "failed", reason or msg("market.no_data")
        else:
            status, reason = "missing", None
        return {
            "symbol": symbol,
            "quote": quote,
            "instrument": saved.get("instrument"),
            "delay": saved.get("delay"),
            "source": saved.get("source", "Yahoo Finance / yfinance") if row else None,
            "bars": bars,
            "chart": {**chart, "start": bars[0]["date"] if bars else None, "end": bars[-1]["date"] if bars else None,
                      "days": HISTORY_DAYS},
            "history": {"status": status, "reason": reason},
        }


def request_history(symbol, force=False):
    """Ask once for six months of daily bars (24 hours); repeated asks add nothing."""
    import uuid

    from iirp.jobs.batches import _create
    from iirp.market.quote_publication import HISTORY_DAYS
    from iirp.market.yahoo import MARKETS, digest

    if symbol not in MARKETS:
        raise NotFoundError("market.symbol_unknown", symbol=symbol)
    today = now().astimezone(ET).date()
    with session() as s, s.begin():
        params = {"kind": "market_quotes", "tickers": [symbol], "history": True,
                  "start_date": (today - timedelta(days=HISTORY_DAYS)).isoformat(), "end_date": today.isoformat()}
        params["request_id"] = "market-history-" + (uuid.uuid4().hex if force else digest([params, today.isoformat()])[:40])
        batch, reused = _create(s, params)
        if not reused:
            batch.title = msg("batch.title.market_history_detail", name=MARKETS[symbol])
    return market_detail(symbol)
