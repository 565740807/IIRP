"""Fact-only Insider overview with explicit P/S, calendar-day and filing-role rules.

Trades group by issuer. The ticker shown and quoted is the first usable symbol
of the issuer's ticker, else of the ticker the filing states. Each trade's
price is checked (see ``iirp.insider.prices``); a "mismatch" price stays
visible but is left out of amount rankings, amount totals and averages, and
every affected list or company reports how many trades were left out.

Every list returns at most its configured number of rows plus its full count.
"""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select

from iirp.analysis.calendar import ET
from iirp.insider.prices import (
    combined_check,
    daily_bars,
    display_ticker,
    effective_price,
    estimate_prices,
    money_text,
    overview_settings,
    percent_text,
    price_check,
    price_comparison,
    quote_symbol,
    shares_text,
)
from iirp.insider.typed import decimal_value
from iirp.insider.views import _owner_view
from iirp.market.stock_quotes import stock_quote_views
from iirp.models import (
    Filing,
    FilingOwner,
    IndexConstituent,
    IndexConstituentState,
    Issuer,
    TransactionEvent,
    now,
)

COMPANY_SORTS = ("net_buy", "net_sell", "buy", "sell", "people")


def index_predicate(issuer_id, ticker, index):
    if index == "all":
        return True
    members = IndexConstituent
    by_cik = select(members.cik).where(members.index_name == index, members.cik.is_not(None))
    by_ticker = select(func.replace(members.ticker, ".", "-")).where(members.index_name == index)
    return or_(issuer_id.in_(by_cik), func.replace(ticker, ".", "-").in_(by_ticker))


def index_status(s):
    from iirp.messages import msg

    states = {item.index_name: item for item in s.scalars(select(IndexConstituentState))}
    result = []
    for name in ("sp500", "nasdaq100"):
        state = states.get(name)
        error = state.error if state else None
        result.append({
            "index_name": name,
            "updated_at": state.updated_at.isoformat() if state and state.updated_at else None,
            "error": msg(error["code"], **error.get("params", {})) if error else None,
        })
    return result


def clusters(rows, days=7, people=2, exclude_plans=False):
    """Newest qualifying window per issuer. Seven days includes d−6 through d.

    Every transaction in the same company/side/window counts once, even if a
    joint filing names multiple owners. Distinct owners count once per window.
    """
    grouped = defaultdict(list)
    for row in rows:
        if not exclude_plans or not row["is_plan"]:
            grouped[row["issuer_id"]].append(row)
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
    """Total of the counted amounts; None when no trade on this side has one."""
    values = [row["counted"] for row in rows if row["counted"] is not None]
    return sum(values, Decimal(0)) if values else None


def _people(rows):
    return len({owner for row in rows for owner in row["owner_ids"]})


def _average(rows, quote):
    known = [row for row in rows if row["counted"] is not None and row["trade_shares"]]
    shares = sum((row["trade_shares"] for row in known), Decimal(0))
    amount = _sum(known)
    average = amount / shares if amount is not None and shares > 0 else None
    return price_comparison(average, quote, estimated=any(row["estimated"] for row in known),
                            check=combined_check(row["check"] for row in known))


def company_totals(rows):
    """Raw per-company numbers; formatting happens once, for returned rows only."""
    buys = [row for row in rows if row["transaction_code"] == "P"]
    sales = [row for row in rows if row["transaction_code"] == "S"]
    buy, sell = _sum(buys), _sum(sales)
    # An absent side is zero; an observed side without any counted amount stays unknown.
    known = (not buys or buy is not None) and (not sales or sell is not None)
    net = (buy or Decimal(0)) - (sell or Decimal(0)) if known else None
    return {"rows": rows, "buys": buys, "sales": sales, "buy": buy, "sell": sell, "net": net,
            "people": _people(rows), "buy_people": _people(buys), "sell_people": _people(sales),
            "start": min(row["transaction_date"] for row in rows),
            "end": max(row["transaction_date"] for row in rows)}


def company_view(totals, quotes):
    rows = totals["rows"]
    first = next((row for row in rows if row["symbol"]), rows[0])
    quote = quotes.get(first["symbol"])
    return {"issuer_id": first["issuer_id"], "name": first["name"], "ticker": first["ticker"],
            "people": totals["people"],
            "buy_people": totals["buy_people"], "sell_people": totals["sell_people"],
            "buy_amount": money_text(totals["buy"]), "sell_amount": money_text(totals["sell"]),
            "net_amount": money_text(totals["net"]),
            "amount_estimated": any(row["estimated"] for row in rows),
            "missing_price_rows": sum(row["amount"] is None for row in rows),
            "price_mismatch_rows": sum(row["mismatch"] for row in rows),
            "buy_price": _average(totals["buys"], quote),
            "sell_price": _average(totals["sales"], quote),
            "start_date": totals["start"].isoformat(), "end_date": totals["end"].isoformat()}


def _descending(value):
    """Sort key part: larger first, unknown last."""
    return (value is None, -(value if value is not None else 0))


def company_sort_key(order):
    def key(totals):
        if order == "net_sell":
            rank = (totals["net"] is None, totals["net"] if totals["net"] is not None else 0)
        elif order == "buy":
            rank = _descending(totals["buy"])
        elif order == "sell":
            rank = _descending(totals["sell"])
        elif order == "people":
            gross = (totals["buy"] or 0) + (totals["sell"] or 0)
            rank = (-totals["people"], *_descending(gross))
        else:
            rank = _descending(totals["net"])
        return (*rank, totals["rows"][0]["issuer_id"])
    return key


def cluster_sort_key(side):
    """Newest window end first, then more insiders, then the larger amount."""
    def key(totals):
        people = totals["sell_people" if side == "S" else "buy_people"]
        amount = totals["sell" if side == "S" else "buy"]
        return (-totals["end"].toordinal(), -people, *_descending(amount),
                totals["rows"][0]["issuer_id"])
    return key


def _section(items, total, mismatched=0):
    return {"total": total, "items": items, "price_mismatch_rows": mismatched}


def _rows(s, *, index, days, role, exclude_plans, at):
    end = at.astimezone(ET).date()
    event = TransactionEvent
    filing_ticker = event.data["ticker"].astext
    statement = select(
        event.id, event.issuer_id, event.version_id, event.transaction_date,
        event.transaction_code, event.trade_shares, event.reported_price, event.is_plan,
        event.is_ceo, event.is_cfo, event.is_president, event.is_chair, event.owner_ids,
        event.price_range_low, event.price_range_high,
        event.data["shares_after"].astext.label("shares_after"),
        Issuer.name, Issuer.ticker.label("issuer_ticker"), filing_ticker.label("filing_ticker"),
    ).join(Issuer, Issuer.id == event.issuer_id).join(
        Filing, Filing.accession == event.accession
    ).where(
        event.status == "CURRENT", Filing.visible.is_(True),
        event.transaction_code.in_(("P", "S")), event.data["table"].astext == "I",
        or_(event.data["currency"].astext.is_(None), event.data["currency"].astext == "USD"),
        event.transaction_date.between(end - timedelta(days=days - 1), end),
        event.transaction_date <= func.date(func.timezone("America/New_York", event.accepted_at)),
        index_predicate(event.issuer_id, func.coalesce(filing_ticker, Issuer.ticker), index),
    )
    if exclude_plans:
        statement = statement.where(event.is_plan.is_(False))
    if role == "executive":
        statement = statement.where(or_(event.is_ceo, event.is_cfo, event.is_president))
    elif role == "director":
        statement = statement.where(event.is_director.is_(True))
    elif role == "ten_percent":
        statement = statement.where(event.is_ten_percent.is_(True))
    return [dict(row) for row in s.execute(statement).mappings()]


def _priced(s, rows, settings):
    """Add display ticker, price, amount and price check to each row; return quotes."""
    for row in rows:
        row["ticker"] = display_ticker(row["issuer_ticker"], row["filing_ticker"])
        row["symbol"] = quote_symbol(row["ticker"])
    bars = daily_bars(s, [(row["symbol"], row["transaction_date"]) for row in rows])
    estimates = estimate_prices(s, (), bars)
    symbols = sorted({row["symbol"] for row in rows} - {None})
    quotes = {item["symbol"]: item for item in stock_quote_views(s, symbols)}
    limit = settings["price_mismatch_ratio"]
    for row in rows:
        price, estimated, low, high = effective_price(row, estimates)
        shares = row["trade_shares"]
        amount = price * shares if price is not None and shares is not None else None
        check = price_check(price, bars.get((row["symbol"], row["transaction_date"])),
                            quotes.get(row["symbol"]), limit=limit)
        mismatch = check["status"] == "mismatch"
        row.update(price=price, estimated=estimated, low=low, high=high, amount=amount,
                   check=check, mismatch=mismatch, counted=None if mismatch else amount)
    return quotes


def _holding_change(row):
    after, shares = decimal_value(row["shares_after"]), row["trade_shares"]
    before = after - shares if after is not None and shares is not None else None
    return shares / before * 100 if before is not None and before > 0 else None


def overview(s, *, index="all", days=30, role="all", min_amount=Decimal(0), exclude_plans=False,
             cluster_days=7, cluster_people=2, exclude_cluster_plans=True,
             company_sort="net_buy", company_limit=None, at=None):
    at = at or now()
    settings = overview_settings()
    company_limit = company_limit or settings["company_page_rows"]
    factor = settings["candidate_factor"]
    rows = _rows(s, index=index, days=days, role=role, exclude_plans=exclude_plans, at=at)
    quotes = _priced(s, rows, settings)
    keys = {(row["ticker"], row["transaction_date"].isoformat(), row["issuer_id"])
            for row in rows if row["price"] is None and row["symbol"]}
    if min_amount > 0:
        rows = [row for row in rows if row["counted"] is not None and row["counted"] >= min_amount]
    buys = [row for row in rows if row["transaction_code"] == "P"]
    sales = [row for row in rows if row["transaction_code"] == "S"]

    def by_amount(side):
        counted = [row for row in side if row["counted"] is not None]
        return sorted(counted, key=lambda row: (row["counted"], row["id"]), reverse=True)

    large_buys, large_sales = by_amount(buys), by_amount(sales)
    executives = sorted(
        (row for row in buys if row["is_ceo"] or row["is_cfo"] or row["is_president"]
         or row["is_chair"]),
        key=lambda row: (row["transaction_date"], row["id"]), reverse=True)
    for row in buys:
        row["holding_change"] = _holding_change(row)
    increases = sorted(
        (row for row in buys if row["holding_change"] is not None and row["holding_change"] >= 20),
        key=lambda row: (row["holding_change"], row["id"]), reverse=True)

    cluster_buys = sorted((company_totals(group) for group in
                           clusters(buys, cluster_days, cluster_people)),
                          key=cluster_sort_key("P"))
    cluster_sales = sorted((company_totals(group) for group in
                            clusters(sales, cluster_days, cluster_people, exclude_cluster_plans)),
                           key=cluster_sort_key("S"))
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["issuer_id"]].append(row)
    companies = sorted((company_totals(group) for group in grouped.values()),
                       key=company_sort_key(company_sort))

    shown = {
        "cluster_buys": cluster_buys[:settings["cluster_rows"]],
        "cluster_sales": cluster_sales[:settings["cluster_rows"]],
        "large_buys": large_buys[:settings["large_rows"]],
        "large_sales": large_sales[:settings["large_rows"]],
        "executive_buys": executives[:settings["executive_rows"]],
        "holding_increases": increases[:settings["holding_rows"]],
        "companies": companies[:company_limit],
    }
    owner_views = _owner_views(s, {row["version_id"] for name in
                                   ("large_buys", "large_sales", "executive_buys",
                                    "holding_increases") for row in shown[name]})

    def trade_view(row):
        owners = owner_views[row["version_id"]]
        return {"id": row["id"], "issuer_id": row["issuer_id"], "name": row["name"],
                "ticker": row["ticker"],
                "owners": [owners.get(owner, {"id": owner, "name": owner, "roles": [],
                                              "entity_type": "unknown"})
                           for owner in row["owner_ids"]],
                "date": row["transaction_date"].isoformat(),
                "shares": shares_text(row["trade_shares"]),
                "amount": money_text(row["counted"]),
                "amount_estimated": row["estimated"],
                "holding_change": percent_text(row.get("holding_change")),
                "price": price_comparison(row["price"], quotes.get(row["symbol"]),
                                          estimated=row["estimated"], low=row["low"],
                                          high=row["high"], check=row["check"])}

    def mismatched(groups):
        return sum(row["mismatch"] for totals in groups for row in totals["rows"])

    # Symbols to quote: every returned row, then unchecked trades that could
    # still enter a ranking (the first candidate_factor × rows of each list).
    wanted = [row["symbol"] for name in ("large_buys", "large_sales", "executive_buys",
                                         "holding_increases") for row in shown[name]]
    wanted += [row["symbol"] for name in ("cluster_buys", "cluster_sales", "companies")
               for totals in shown[name] for row in totals["rows"][:1]]
    candidates = [*large_buys[:factor * settings["large_rows"]],
                  *large_sales[:factor * settings["large_rows"]],
                  *(row for group in (cluster_buys[:factor * settings["cluster_rows"]],
                                      cluster_sales[:factor * settings["cluster_rows"]],
                                      companies[:factor * company_limit])
                    for totals in group for row in totals["rows"])]
    wanted += [row["symbol"] for row in candidates if row["check"]["status"] == "unchecked"]
    quote_symbols = list(dict.fromkeys(symbol for symbol in wanted if symbol))
    quote_symbols = quote_symbols[:settings["max_quote_symbols"]]
    pending = sum(quotes.get(symbol, {}).get("fetched_at") is None for symbol in quote_symbols)

    return {
        "as_of": at.isoformat(), "indices": index_status(s),
        "cluster_buys": _section([company_view(item, quotes) for item in shown["cluster_buys"]],
                                 len(cluster_buys), mismatched(cluster_buys)),
        "cluster_sales": _section([company_view(item, quotes) for item in shown["cluster_sales"]],
                                  len(cluster_sales), mismatched(cluster_sales)),
        "large_buys": _section([trade_view(row) for row in shown["large_buys"]],
                               len(large_buys), sum(row["mismatch"] for row in buys)),
        "large_sales": _section([trade_view(row) for row in shown["large_sales"]],
                                len(large_sales), sum(row["mismatch"] for row in sales)),
        "executive_buys": _section([trade_view(row) for row in shown["executive_buys"]],
                                   len(executives)),
        "holding_increases": _section([trade_view(row) for row in shown["holding_increases"]],
                                      len(increases)),
        "companies": _section([company_view(item, quotes) for item in shown["companies"]],
                              len(companies), sum(row["mismatch"] for row in rows)),
        "company_sort": company_sort, "company_page_rows": settings["company_page_rows"],
        "price_keys": [{"ticker": ticker, "date": day, "issuer_id": issuer}
                       for ticker, day, issuer in sorted(keys)],
        "quote_symbols": quote_symbols, "quotes_pending": pending,
    }


def _owner_views(s, versions):
    views = defaultdict(dict)
    if versions:
        for item in s.scalars(select(FilingOwner).where(FilingOwner.version_id.in_(versions))):
            views[item.version_id][item.owner_id] = _owner_view(item.owner_id, item.relationship)
    return views
