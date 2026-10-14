"""Read projections from committed local facts; none of these reads fetch a source."""

from sqlalchemy import select

from iirp.config import refresh
from iirp.db import session
from iirp.jobs.providers import sec_configured
from iirp.market.yahoo import MARKETS, source_contract
from iirp.messages import UserError, msg
from iirp.models import Issuer, Owner, Security, now


def home():
    """Saved quotes of the strip instruments shown now; the browser asks /freshness/ensure for new ones."""
    from iirp.display import strip_selection
    from iirp.market.instruments import shown_symbols
    from iirp.market.reads import quote_views
    from iirp.models import MarketQuote

    current = now()
    with session() as s:
        selected = strip_selection(s)
        saved = {row.symbol: row.data for row in s.scalars(select(MarketQuote).where(MarketQuote.symbol.in_(selected)))}
        shown = shown_symbols(selected, saved, current)
        market = quote_views(s, {symbol: MARKETS[symbol] for symbol in shown}, current)
    browser = refresh()["browser"]
    return {"observed_at": current, "market": market, "refresh": browser}


def feed(session_id="", cursor="", kind="all", order="transaction", index="all", watchlist=False):
    from iirp.insider.feed import feed as read_feed
    from iirp.insider.feed_updates import pending_feed_metadata
    from iirp.market.classification import Memberships

    with session() as s, s.begin():
        result = read_feed(s, session_id, cursor, kind, order, index, watchlist)
        # Index marks are read now, not from the group revision, so they follow the weekly lists.
        Memberships(s).mark(result["groups"])
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
        items = alias_items(s, q)
        for security in s.scalars(
            select(Security)
            .where(Security.symbol.ilike(term) | Security.name.ilike(term))
            .limit(20)
        ):
            # Already found through an alias, with its company.
            if any(x["id"] in (security.issuer_id, security.id) or security.symbol in x["ticker"].split(", ")
                   for x in items if x.get("alias")):
                continue
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
                        "notable_key": f"sec:{entity.id}" if kind == "person" else None,
                    }
                )
        items += politician_items(s, term)
        return {
            "items": items,
            "data": {"message": msg("search.scope")},
        }


def politician_items(s, term):
    """House members and OGE filers kept locally whose name matches; they open the person page."""
    from urllib.parse import quote

    from iirp.models.political import PoliticalPerson
    from iirp.notable.people import person_key

    items, seen = [], set()
    for person in s.scalars(select(PoliticalPerson).where(PoliticalPerson.source.in_(("house", "oge")),
                                                          PoliticalPerson.name.ilike(term))
                            .order_by(PoliticalPerson.name).limit(20)):
        key = person_key(person.source, name=person.name, district=person.district, position=person.position)
        if key in seen:
            continue
        seen.add(key)
        source, _, identity = key.partition(":")
        items.append({"id": key, "kind": "politician", "name": person.name,
                      "detail": person.district if person.source == "house" else person.position,
                      "href": f"/notable/{source}/{quote(identity, safe='')}", "notable_key": key})
    return items


def alias_items(s, q):
    """Stocks the term names through config/aliases.toml ("Google" → GOOGL, GOOG), first in the results.

    A code's company is its security's issuer, else the issuer the index lists
    name for it; share classes of one company make one item. A code without a
    saved security or issuer opens its monthly analysis.
    """
    from iirp.aliases import code_key, matches
    from iirp.models import IndexConstituent

    items: list[dict] = []
    for alias, codes in matches(q):
        securities = {row.symbol: row for row in s.scalars(select(Security).where(Security.symbol.in_(codes)))}
        # The S&P 500 list's CIK first: the lists can disagree for a share class.
        listed: dict[str, str] = {}
        for row in sorted(s.scalars(select(IndexConstituent).where(
                IndexConstituent.ticker.in_([*codes, *(code.replace("-", ".") for code in codes)]))),
                key=lambda row: row.index_name != "sp500"):
            if row.cik:
                listed.setdefault(code_key(row.ticker), row.cik)
        for code in codes:
            security = securities.get(code)
            issuer_id = (security.issuer_id if security else None) or listed.get(code)
            issuer = s.get(Issuer, issuer_id) if issuer_id else None
            same = next((item for item in items if issuer and item["id"] == issuer.id), None)
            if same:
                if code not in same["ticker"].split(", "):
                    same["ticker"] += ", " + code
                continue
            if issuer is None and any(item["ticker"] == code for item in items):
                continue
            items.append({
                "id": issuer.id if issuer else (security.id if security else code),
                "kind": "company" if issuer else "security",
                "name": issuer.name if issuer else (security.name if security and security.name else code),
                "ticker": code, "security_id": security.id if security else None,
                "issuer_id": issuer.id if issuer else None, "alias": alias,
                "href": "/companies/" + issuer.id if issuer else "/analysis/monthly?tickers=" + code,
            })
    return items


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
