"""Fact-only Insider overview with explicit P/S, calendar-day and filing-role rules."""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select

from iirp.analysis.calendar import ET
from iirp.insider.prices import effective_price, estimate_prices, price_comparison
from iirp.insider.typed import decimal_value
from iirp.models import (
    Filing,
    IndexConstituent,
    IndexConstituentState,
    Issuer,
    TransactionEvent,
    now,
)


def index_predicate(issuer_id, ticker, index):
    if index == "all":
        return True
    members = IndexConstituent
    return or_(issuer_id.in_(select(members.cik).where(members.index_name == index, members.cik.is_not(None))),
               func.replace(ticker, ".", "-").in_(select(func.replace(members.ticker, ".", "-")).where(members.index_name == index)))


def index_status(s):
    from iirp.messages import msg
    states = {item.index_name: item for item in s.scalars(select(IndexConstituentState))}
    return [{"index_name": name, "updated_at": states[name].updated_at.isoformat() if name in states and states[name].updated_at else None,
             "error": msg(states[name].error["code"], **states[name].error.get("params", {})) if name in states and states[name].error else None}
            for name in ("sp500", "nasdaq100")]


def clusters(rows, days=7, people=2, exclude_plans=False):
    """Newest qualifying window per issuer. Seven days includes d−6 through d.

    Every transaction in the same company/side/window counts once, even if a
    joint filing names multiple owners. Distinct owners count once per window.
    """
    grouped = defaultdict(list)
    for row in rows:
        if not exclude_plans or not row["is_plan"]:
            grouped[(row["issuer_id"], row["ticker"])].append(row)
    result = []
    for trades in grouped.values():
        trades.sort(key=lambda row: (row["transaction_date"], row["id"]))
        for end in sorted({row["transaction_date"] for row in trades}, reverse=True):
            first = end - timedelta(days=days - 1)
            window = [row for row in trades if first <= row["transaction_date"] <= end]
            if len({owner for row in window for owner in row["owner_ids"]}) >= people:
                result.append(window)
                break
    return result


def _sum(rows):
    values = [row["amount"] for row in rows if row["amount"] is not None]
    return sum(values, Decimal(0)) if values else None


def _average(rows, quote):
    known = [row for row in rows if row["price"] is not None and row["trade_shares"] is not None]
    shares = sum((row["trade_shares"] for row in known), Decimal(0))
    amount = _sum(known)
    return price_comparison(amount / shares if amount is not None and shares > 0 else None, quote,
                            estimated=any(row["estimated"] for row in known))


def company_view(rows, quotes):
    first = rows[0]
    buys = [row for row in rows if row["transaction_code"] == "P"]
    sales = [row for row in rows if row["transaction_code"] == "S"]
    buy, sell = _sum(buys), _sum(sales)
    # An absent side is zero; an observed side with unknown prices stays unknown.
    net = (buy or Decimal(0)) - (sell or Decimal(0)) if (not buys or buy is not None) and (not sales or sell is not None) else None
    quote = quotes.get((first["ticker"] or "").replace(".", "-"))
    return {"issuer_id": first["issuer_id"], "name": first["name"], "ticker": first["ticker"],
            "buy_people": len({owner for row in buys for owner in row["owner_ids"]}),
            "sell_people": len({owner for row in sales for owner in row["owner_ids"]}),
            "buy_amount": str(buy) if buy is not None else None,
            "sell_amount": str(sell) if sell is not None else None,
            "net_amount": str(net) if net is not None else None,
            "amount_estimated": any(row["estimated"] for row in rows),
            "missing_price_rows": sum(row["amount"] is None for row in rows),
            "buy_price": _average(buys, quote), "sell_price": _average(sales, quote),
            "start_date": min(row["transaction_date"] for row in rows).isoformat(),
            "end_date": max(row["transaction_date"] for row in rows).isoformat()}


def overview(s, *, index="all", days=30, role="all", min_amount=Decimal(0), exclude_plans=False,
             cluster_days=7, cluster_people=2, exclude_cluster_plans=True, at=None):
    from iirp.analysis.insider_windows import provider_symbol
    from iirp.insider.views import _owner_view
    from iirp.market.stock_quotes import stock_quote_views
    from iirp.models import FilingOwner

    at = at or now()
    end = at.astimezone(ET).date()
    event = TransactionEvent
    ticker = func.coalesce(event.data["ticker"].astext, Issuer.ticker)
    statement = select(
        event.id, event.issuer_id, event.version_id, event.transaction_date, event.transaction_code,
        event.trade_shares, event.reported_price, event.reported_amount, event.is_plan,
        event.is_ceo, event.is_cfo, event.is_president, event.is_chair, event.owner_ids,
        event.price_range_low, event.price_range_high,
        event.data["shares_after"].astext.label("shares_after"),
        Issuer.name, ticker.label("ticker"),
    ).join(Issuer, Issuer.id == event.issuer_id).join(Filing, Filing.accession == event.accession).where(
        event.status == "CURRENT", Filing.visible.is_(True),
        event.transaction_code.in_(("P", "S")), event.data["table"].astext == "I",
        or_(event.data["currency"].astext.is_(None), event.data["currency"].astext == "USD"),
        event.transaction_date.between(end - timedelta(days=days - 1), end),
        event.transaction_date <= func.date(func.timezone("America/New_York", event.accepted_at)),
        index_predicate(event.issuer_id, ticker, index),
    )
    if exclude_plans:
        statement = statement.where(event.is_plan.is_(False))
    if role == "executive":
        statement = statement.where(or_(event.is_ceo, event.is_cfo, event.is_president))
    elif role == "director":
        statement = statement.where(event.is_director.is_(True))
    elif role == "ten_percent":
        statement = statement.where(event.is_ten_percent.is_(True))
    rows = [dict(row) for row in s.execute(statement).mappings()]
    estimates = estimate_prices(s, [(row["ticker"], row["transaction_date"]) for row in rows if row["reported_price"] is None])
    keys = []
    for row in rows:
        price, estimated, low, high = effective_price(row, estimates)
        row.update(price=price, estimated=estimated, low=low, high=high,
                   amount=price * row["trade_shares"] if price is not None and row["trade_shares"] is not None else None)
        if price is None and row["ticker"]:
            keys.append({"ticker": row["ticker"], "date": row["transaction_date"].isoformat(), "issuer_id": row["issuer_id"]})
    if min_amount > 0:
        rows = [row for row in rows if row["amount"] is not None and row["amount"] >= min_amount]
    symbols = {provider_symbol(row["ticker"]) for row in rows} - {None}
    quotes = {item["symbol"]: item for item in stock_quote_views(s, sorted(symbols))}
    buys = [row for row in rows if row["transaction_code"] == "P"]
    sales = [row for row in rows if row["transaction_code"] == "S"]
    large_buys = sorted((row for row in buys if row["amount"] is not None), key=lambda row: (row["amount"], row["id"]), reverse=True)[:10]
    large_sales = sorted((row for row in sales if row["amount"] is not None), key=lambda row: (row["amount"], row["id"]), reverse=True)[:10]
    executives = sorted((row for row in buys if row["is_ceo"] or row["is_cfo"] or row["is_president"] or row["is_chair"]), key=lambda row: (row["transaction_date"], row["id"]), reverse=True)
    increases = []
    for row in buys:
        after, shares = decimal_value(row["shares_after"]), row["trade_shares"]
        before = after - shares if after is not None and shares is not None else None
        row["holding_change"] = shares / before * 100 if before is not None and before > 0 else None
        if row["holding_change"] is not None and row["holding_change"] >= 20:
            increases.append(row)
    increases.sort(key=lambda row: (row["holding_change"], row["id"]), reverse=True)
    displayed = {row["id"]: row for group in (large_buys, large_sales, executives, increases) for row in group}
    versions = {row["version_id"] for row in displayed.values()}
    owner_views = defaultdict(dict)
    if versions:
        for item in s.scalars(select(FilingOwner).where(FilingOwner.version_id.in_(versions))):
            owner_views[item.version_id][item.owner_id] = _owner_view(item.owner_id, item.relationship)

    def trade_view(row):
        return {"id": row["id"], "issuer_id": row["issuer_id"], "name": row["name"], "ticker": row["ticker"],
                "owners": [owner_views[row["version_id"]].get(owner, {"id": owner, "name": owner, "roles": [], "entity_type": "unknown"}) for owner in row["owner_ids"]],
                "date": row["transaction_date"].isoformat(),
                "shares": str(row["trade_shares"]) if row["trade_shares"] is not None else None,
                "amount": str(row["amount"]) if row["amount"] is not None else None,
                "amount_estimated": row["estimated"],
                "holding_change": str(row.get("holding_change")) if row.get("holding_change") is not None else None,
                "price": price_comparison(row["price"], quotes.get((row["ticker"] or "").replace(".", "-")),
                                           estimated=row["estimated"], low=row["low"], high=row["high"])}

    companies = defaultdict(list)
    for row in rows:
        companies[(row["issuer_id"], row["ticker"])].append(row)
    company_rows = [company_view(group, quotes) for group in companies.values()]
    company_rows.sort(key=lambda row: (Decimal(row["net_amount"]) if row["net_amount"] is not None else Decimal("-Infinity"), row["issuer_id"]), reverse=True)
    return {"as_of": at.isoformat(), "indices": index_status(s),
            "cluster_buys": [company_view(group, quotes) for group in clusters(buys, cluster_days, cluster_people)],
            "cluster_sales": [company_view(group, quotes) for group in clusters(sales, cluster_days, cluster_people, exclude_cluster_plans)],
            "large_buys": [trade_view(row) for row in large_buys], "large_sales": [trade_view(row) for row in large_sales],
            "executive_buys": [trade_view(row) for row in executives], "holding_increases": [trade_view(row) for row in increases],
            "companies": company_rows, "price_keys": list({(key["ticker"], key["date"], key["issuer_id"]): key for key in keys}.values())}
