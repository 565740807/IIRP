"""Read projections from committed local facts; none of these reads fetch a source."""

from datetime import datetime, timedelta

from sqlalchemy import select

from iirp.config import refresh
from iirp.db import session
from iirp.jobs.providers import sec_configured
from iirp.market.yahoo import MARKETS, source_contract
from iirp.messages import UserError, msg
from iirp.models import Issuer, Job, MarketQuote, Owner, Security, now


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
        overdue = refresh()["quotes"]["overdue_seconds"]
        if (current - fetched).total_seconds() > overdue:
            return "delayed", msg("home.quote.overdue", minutes=overdue // 60)
        return "live", None
    if end and current >= end and fetched < end:
        return "delayed", msg("home.quote.close_pending")
    if start and end:
        return "closed", None
    return ("delayed", msg("quote.delay.daily")) if quote.get("status") == "DAILY" else ("live", None)


def home():
    """Saved quotes for the home strip; the browser asks /freshness/ensure for new ones."""
    current = now()
    with session() as s:
        quotes = {q.symbol: q for q in s.scalars(select(MarketQuote).where(MarketQuote.symbol.in_(MARKETS)))}
        # The newest quote job per symbol: a failure after the last fetch explains a delay.
        latest = {}
        for job in s.scalars(
            select(Job).where(Job.kind == "market_quote", Job.created_at > current - timedelta(days=1))
            .order_by(Job.created_at.desc()).limit(50)
        ):
            latest.setdefault(job.target.get("symbol"), job)
        market = []
        for symbol, name in MARKETS.items():
            row, job = quotes.get(symbol), latest.get(symbol)
            failure = job if job is not None and job.status in ("FAILED", "RETRY_WAIT") else None
            if row is None:
                market.append({"symbol": symbol, "name": name, "status": "NOT_FETCHED", "freshness": "missing",
                               "reason": failure.error if failure else msg("home.quote_not_fetched")})
                continue
            quote = {**row.data, "name": name, "fetched_at": row.fetched_at.isoformat()}
            quote["freshness"], quote["reason"] = _quote_freshness(quote, failure, current)
            market.append(quote)
    browser = refresh()["browser"]
    return {"observed_at": current, "market": market, "refresh": browser}


def feed(session_id="", cursor="", kind="all", order="transaction"):
    from iirp.insider.feed import feed as read_feed
    from iirp.insider.feed_updates import pending_feed_metadata

    with session() as s, s.begin():
        result = read_feed(s, session_id, cursor, kind, order)
        # Polling has its own scalar endpoint; ordinary page reads do not repeat
        # the full latest-revision scan just to report an update count.
        result["new_count"] = 0
        if not cursor:
            # Later pages only append groups; the summary comes with the first
            # page and with every update check.
            result["pending_summary"], result["pending_filings"] = pending_feed_metadata(s, preview=True)
        return result


def search(q):
    q = q.strip()
    if not q:
        return {
            "items": [],
            "data": {"message": msg("search.hint")},
        }
    if len(q) > 100:
        raise UserError("search.too_long", max=100)
    term = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with session() as s:
        items = []
        for security in s.scalars(
            select(Security)
            .where(Security.symbol.ilike(term) | Security.name.ilike(term))
            .limit(20)
        ):
            items.append(
                {
                    "id": security.issuer_id or security.id,
                    "kind": "company" if security.issuer_id else "security",
                    "name": security.name or security.symbol,
                    "ticker": security.symbol,
                    "security_id": security.id,
                    "issuer_id": security.issuer_id,
                    "href": "/companies/" + security.issuer_id
                    if security.issuer_id
                    else "/analysis/monthly?tickers=" + security.symbol,
                }
            )
        for model, kind in ((Issuer, "company"), (Owner, "person")):
            for entity in s.scalars(
                select(model).where(model.name.ilike(term) | model.id.ilike(term)).limit(20)
            ):
                if any(x["kind"] == kind and x["id"] == entity.id for x in items):
                    continue
                items.append(
                    {
                        "id": entity.id,
                        "kind": kind,
                        "name": entity.name,
                        "href": ("/companies/" if kind == "company" else "/people/") + entity.id,
                    }
                )
        return {
            "items": items,
            "data": {"message": msg("search.scope")},
        }


def providers():
    contract = source_contract()
    return {
        "items": [
            {
                "id": "sec",
                "name": msg("provider.sec.name"),
                "configured": sec_configured(),
                "status": "CONFIGURED" if sec_configured() else "NEEDS_CONFIG",
                "message": msg("provider.sec.configured" if sec_configured()
                               else "provider.sec.needs_config"),
                "budget": msg("provider.sec.budget"),
            },
            {
                "id": "yfinance",
                "name": msg("provider.yahoo.name"),
                "configured": True,
                "status": "AVAILABLE" if contract["verified"] else "UNVERIFIED",
                "message": msg("provider.yahoo.verified" if contract["verified"]
                               else "provider.yahoo.unverified"),
                "budget": msg("provider.yahoo.budget"),
            },
        ],
        "data": {},
    }
