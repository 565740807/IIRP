"""Open-market Insider activity per stock code, for small marks beside a code.

The rules are the overview's (``iirp.insider.overview``): open-market purchases
and sales (P/S) of the non-derivative table in US dollars, from visible filings,
dated by the trade over the last ``days`` calendar days ending today (US Eastern),
and never dated after their own acceptance day. Amounts are the effective price
times the shares; a trade with a suspect price is counted as a trade but left out
of the amounts, as in the overview. 10b5-1 plan trades count like any other and
are also reported on their own.

A trade belongs to a requested code when its issuer's ticker or the ticker its
filing states names that code (any class of a multi-class text); "." and "-" are
the same. Everything is read from the local database: no quote, price or
identity request is made for these marks.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select

from iirp.analysis.calendar import ET
from iirp.insider.overview import (
    _owner_views,
    _people,
    _priced,
    _rows,
    _sum,
    company_sectors,
    company_totals,
    ticker_tokens,
)
from iirp.insider.prices import money_text, overview_settings
from iirp.models import Owner, now

MAX_TICKERS = 200
TOP_TRADERS = 3


def ticker_key(value):
    """The form codes are compared in: upper case, "." read as "-"."""
    return (value or "").strip().upper().replace(".", "-")


def _side(rows):
    return {"people": _people(rows), "trades": len(rows), "amount": money_text(_sum(rows))}


def _largest(rows):
    """Each owner's trades on each side, largest counted amount first (unknown last): the
    first TOP_TRADERS of them, and how many owner-side pairs there are."""
    grouped = defaultdict(list)
    for row in rows:
        for owner in row["owner_ids"]:
            grouped[(owner, row["transaction_code"])].append(row)
    ordered = sorted(grouped.items(), key=lambda item: (
        _sum(item[1]) is None, -(_sum(item[1]) or 0),
        -max(row["transaction_date"] for row in item[1]).toordinal(), item[0]))
    return ordered[:TOP_TRADERS], len(grouped)


def _trader_views(s, shown):
    """Names and roles of every shown trader, read once for all codes: the name as filed,
    else the saved owner's name."""
    views = _owner_views(s, {row["version_id"] for _, trades in shown for row in trades})
    owners = {owner for (owner, _), _ in shown}
    names = dict(s.execute(select(Owner.id, Owner.name).where(Owner.id.in_(owners))).all()) if owners else {}

    def view(owner, code, trades):
        latest = max(trades, key=lambda row: (row["transaction_date"], row["id"]))
        filed = views[latest["version_id"]].get(owner, {"id": owner, "name": owner, "roles": []})
        return {"id": owner, "name": filed["name"] if filed["name"] != owner else names.get(owner) or owner,
                "roles": filed["roles"], "side": "buy" if code == "P" else "sell", "trades": len(trades),
                "amount": money_text(_sum(trades)), "last_date": latest["transaction_date"].isoformat()}
    return view


def _summary(rows, traders):
    buys = [row for row in rows if row["transaction_code"] == "P"]
    sales = [row for row in rows if row["transaction_code"] == "S"]
    buy, sell = _sum(buys), _sum(sales)
    # As in the overview: an absent side is zero, a side without any counted amount is unknown.
    known = (not buys or buy is not None) and (not sales or sell is not None)
    net = (buy or Decimal(0)) - (sell or Decimal(0)) if known else None
    latest = max(rows, key=lambda row: (row["transaction_date"], row["id"]))
    shown, count = traders
    return {"issuer_id": latest["issuer_id"], "name": latest["name"], "ticker": latest["ticker"],
            "buy": _side(buys), "sell": _side(sales), "net_amount": money_text(net),
            "last_date": latest["transaction_date"].isoformat(),
            "plan_trades": sum(bool(row["is_plan"]) for row in rows),
            "amount_estimated": any(row["estimated"] for row in rows if row["counted"] is not None),
            "missing_price_rows": sum(row["amount"] is None for row in rows),
            "price_mismatch_rows": sum(row["mismatch"] for row in rows),
            "traders": shown, "trader_count": count}


def activity(s, tickers, *, days=30, at=None):
    """Activity of each requested code that has any; codes without trades are left out."""
    at = at or now()
    keys = sorted({key for key in map(ticker_key, tickers) if key})
    end = at.astimezone(ET).date()
    rows = _rows(s, days=days, at=at, tickers=keys) if keys else []
    _priced(s, rows, overview_settings())
    wanted = set(keys)
    grouped = defaultdict(list)
    for row in rows:
        for key in ticker_tokens(row["issuer_ticker"], row["filing_ticker"]) & wanted:
            grouped[key].append(row)
    largest = {key: _largest(grouped[key]) for key in keys if grouped[key]}
    view = _trader_views(s, [pair for shown, _ in largest.values() for pair in shown])
    items = {key: _summary(grouped[key], ([view(owner, code, trades) for (owner, code), trades in shown], count))
             for key, (shown, count) in largest.items()}
    return {"as_of": at.isoformat(), "days": days,
            "start_date": (end - timedelta(days=days - 1)).isoformat(), "end_date": end.isoformat(),
            "items": items}


def _totals(rows):
    """Amounts of a set of companies' trades, with the overview's unknown-side rule."""
    issuers = {row["issuer_id"] for row in rows}
    if not rows:
        return {"companies": 0, "buy_companies": 0, "sell_companies": 0, "buy_amount": None,
                "sell_amount": None, "net_amount": None, "amount_estimated": False,
                "missing_price_rows": 0, "price_mismatch_rows": 0}
    totals = company_totals(rows)
    return {"companies": len(issuers),
            "buy_companies": len({row["issuer_id"] for row in totals["buys"]}),
            "sell_companies": len({row["issuer_id"] for row in totals["sales"]}),
            "buy_amount": money_text(totals["buy"]), "sell_amount": money_text(totals["sell"]),
            "net_amount": money_text(totals["net"]),
            "amount_estimated": any(row["estimated"] for row in rows if row["counted"] is not None),
            "missing_price_rows": sum(row["amount"] is None for row in rows),
            "price_mismatch_rows": sum(row["mismatch"] for row in rows)}


def sector_activity(s, *, days=30, at=None):
    """Open-market purchases and sales of the last ``days`` days by each company's
    current, saved sector and industry group (``company_sectors``).

    Companies without a saved sector are reported as unclassified; industry groups
    count only companies with a GICS sub-industry, and each sector says how many of
    its companies have none.
    """
    from iirp.analysis.sectors import industry_groups, sector_ids

    at = at or now()
    end = at.astimezone(ET).date()
    rows = _rows(s, days=days, at=at)
    _priced(s, rows, overview_settings())
    classes = company_sectors(s, rows)
    by_sector, by_group, unclassified = defaultdict(list), defaultdict(list), []
    for row in rows:
        sector, group = classes[row["issuer_id"]]
        if sector is None:
            unclassified.append(row)
            continue
        by_sector[sector].append(row)
        if group:
            by_group[group].append(row)
    ungrouped = defaultdict(set)
    for issuer, (sector, group) in classes.items():
        if sector and not group:
            ungrouped[sector].add(issuer)
    return {"as_of": at.isoformat(), "days": days,
            "start_date": (end - timedelta(days=days - 1)).isoformat(), "end_date": end.isoformat(),
            "companies": len(classes),
            "sectors": [{"id": sector, **_totals(by_sector[sector]), "ungrouped": len(ungrouped[sector])}
                        for sector in sector_ids()],
            "groups": [{"id": group["id"], "sector": group["sector"], **_totals(by_group[group["id"]])}
                       for group in industry_groups()],
            "unclassified": _totals(unclassified)}


NEWS_DAYS = 30
NEWS_TRADERS = 5


def _news_lines(trades, owner):
    """One line per insider and side: the trades of the same filing owners on the same side,
    their counted amount (the overview's rule: rows with a suspect or missing price are left out),
    how many there are and their trade days. Largest amount first; lines without any counted
    amount last. Each line opens its newest trade."""
    grouped = defaultdict(list)
    for row in trades:
        grouped[(tuple(row["owner_ids"]), row["transaction_code"])].append(row)
    lines = []
    for (owners, code), rows in grouped.items():
        counted = [row["counted"] for row in rows if row["counted"] is not None]
        total = sum(counted, Decimal(0)) if counted else None
        newest = max(rows, key=lambda row: (row["transaction_date"], row["first_accepted_at"], row["id"]))
        days = sorted(row["transaction_date"] for row in rows)
        lines.append((total, newest, {
            "id": newest["id"], "side": "buy" if code == "P" else "sell",
            "owners": [owner(newest, identifier) for identifier in owners],
            "amount": money_text(total), "amount_partial": total is not None and len(counted) < len(rows),
            "amount_estimated": any(row["estimated"] for row in rows if row["counted"] is not None),
            "price_mismatch": any(row["mismatch"] for row in rows), "trades": len(rows),
            "first_date": days[0].isoformat(), "last_date": days[-1].isoformat(),
            "accepted_at": max(row["first_accepted_at"] for row in rows).isoformat()}))
    lines.sort(key=lambda line: (line[0] is None, -(line[0] or 0), -line[1]["transaction_date"].toordinal(),
                                 line[2]["owners"][0]["name"] if line[2]["owners"] else "", line[2]["id"]))
    return [line for _, _, line in lines]


def new_trades(s, since, *, at=None):
    """Open-market purchases and sales of watched codes that are new: accepted by the SEC
    after the code's start time (``since``: code → instant) and no later than ``at``.

    The rows are the overview's (non-derivative P/S in US dollars from visible filings,
    current versions, never dated after their own acceptance day), matched to a code as
    ``activity`` matches them. A transaction that corrects an earlier one carries the
    original's acceptance time, so a correction is never news a second time. At most the
    last ``NEWS_DAYS`` days are read. Local reads only.
    """
    from iirp.insider.overview import _owner_views, overview_rows_clause, states_any
    from iirp.models import Filing, Issuer, TransactionEvent

    at = at or now()
    keys = sorted(since)
    if not keys:
        return []
    floor = max(min(since.values()), at - timedelta(days=NEWS_DAYS))
    event = TransactionEvent
    # Every column and condition is covered by ix_event_overview, so the candidates come
    # from that index alone; the correction time (JSON) is read for the matched rows only.
    statement = select(
        event.id, event.issuer_id, event.version_id, event.transaction_date, event.accepted_at,
        event.transaction_code, event.trade_shares, event.reported_price, event.is_plan, event.owner_ids,
        event.price_range_low, event.price_range_high, Issuer.name, Issuer.ticker.label("issuer_ticker"),
        event.filing_ticker,
    ).join(Issuer, Issuer.id == event.issuer_id).join(Filing, Filing.accession == event.accession).where(
        overview_rows_clause(), Filing.visible.is_(True), event.accepted_at > floor, event.accepted_at <= at,
        event.transaction_date <= func.date(func.timezone("America/New_York", event.accepted_at)),
        states_any(func.concat_ws(" ", Issuer.ticker, event.filing_ticker), keys),
    )
    rows = [dict(row) for row in s.execute(statement).mappings()]
    originals = dict(s.execute(select(event.id, event.data["original_accepted_at"].astext).where(
        event.id.in_([row["id"] for row in rows]))).all()) if rows else {}
    grouped = defaultdict(list)
    for row in rows:
        original = originals.get(row["id"])
        first = datetime.fromisoformat(original) if original else row["accepted_at"]
        if first.tzinfo is None:
            first = first.replace(tzinfo=timezone.utc)
        row["first_accepted_at"] = first
        for key in ticker_tokens(row["issuer_ticker"], row["filing_ticker"]) & set(keys):
            if since[key] < first <= at:
                grouped[key].append(row)
    shown = [row for trades in grouped.values() for row in trades]
    _priced(s, shown, overview_settings())
    views = _owner_views(s, {row["version_id"] for row in shown})
    owners = {owner for row in shown for owner in row["owner_ids"]}
    names = dict(s.execute(select(Owner.id, Owner.name).where(Owner.id.in_(owners))).all()) if owners else {}

    def owner(row, identifier):
        filed = views[row["version_id"]].get(identifier)
        return {"id": identifier, "name": filed["name"] if filed and filed["name"] != identifier
                else names.get(identifier) or identifier}

    result = []
    for key, trades in grouped.items():
        trades.sort(key=lambda row: (row["first_accepted_at"], row["transaction_date"], row["id"]), reverse=True)
        latest = trades[0]
        lines = _news_lines(trades, owner)
        result.append({
            "ticker": key, "issuer_id": latest["issuer_id"], "name": latest["name"],
            "since": since[key].isoformat(), "latest_at": latest["first_accepted_at"].isoformat(),
            "trades": len(trades), "buys": sum(row["transaction_code"] == "P" for row in trades),
            "sales": sum(row["transaction_code"] == "S" for row in trades),
            "traders": lines[:NEWS_TRADERS], "trader_count": len(lines),
        })
    result.sort(key=lambda item: (item["latest_at"], item["ticker"]), reverse=True)
    return result
