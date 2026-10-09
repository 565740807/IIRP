"""Read projections from committed local facts; none of these reads fetch a source."""

from sqlalchemy import select

from iirp.config import refresh
from iirp.db import session
from iirp.jobs.providers import sec_configured
from iirp.market.yahoo import MARKETS, source_contract
from iirp.messages import UserError, msg
from iirp.models import Issuer, Owner, Security, now


def home():
    """Saved quotes for the home strip; the browser asks /freshness/ensure for new ones."""
    from iirp.market.reads import quote_views

    current = now()
    with session() as s:
        market = quote_views(s, MARKETS, current)
    browser = refresh()["browser"]
    return {"observed_at": current, "market": market, "refresh": browser}


def feed(session_id="", cursor="", kind="all", order="transaction", index="all"):
    from iirp.insider.feed import feed as read_feed
    from iirp.insider.feed_updates import pending_feed_metadata

    with session() as s, s.begin():
        result = read_feed(s, session_id, cursor, kind, order, index)
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
