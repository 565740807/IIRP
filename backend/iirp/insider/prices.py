"""Insider display prices from reported facts and the existing expiring daily cache."""

from decimal import Decimal

from sqlalchemy import select, tuple_

from iirp.insider.typed import decimal_value
from iirp.models import PriceCache, PriceCacheBar, Security, now


def estimate_prices(s, keys):
    keys = {(ticker.replace(".", "-"), day) for ticker, day in keys if ticker and day}
    if not keys:
        return {}
    rows = s.execute(select(Security.symbol, PriceCacheBar.session_date,
                            PriceCacheBar.high, PriceCacheBar.low, PriceCacheBar.close)
                     .join(PriceCache, PriceCache.security_id == Security.id)
                     .join(PriceCacheBar, PriceCacheBar.cache_id == PriceCache.id)
                     .where(PriceCache.expires_at > now(), PriceCacheBar.status == "VALID",
                            tuple_(Security.symbol, PriceCacheBar.session_date).in_(sorted(keys)))
                     .order_by(Security.id)).all()
    result = {}
    for ticker, day, high, low, close in rows:
        if all(value is not None for value in (high, low, close)) and 0 <= low <= close <= high:
            result.setdefault((ticker, day), ((high + low + close) / Decimal(3), low, high))
    return result


def price_comparison(price, quote=None, *, estimated=False, low=None, high=None, amount=None, amount_estimated=False):
    current = decimal_value((quote or {}).get("value"))
    price = decimal_value(price)
    change = (current / price - 1) * 100 if current is not None and price is not None and price > 0 else None
    relation = "near" if change is not None and abs(change) <= 1 else "higher" if change is not None and change > 1 else "lower" if change is not None else None
    status = {"LIVE": "live", "CLOSED": "closed", "STALE": "delayed", "NOT_FETCHED": "missing"}.get((quote or {}).get("status"), "missing")
    return {"price": str(price) if price is not None else None, "estimated": estimated,
            "range_low": str(low) if low is not None else None, "range_high": str(high) if high is not None else None,
            "current_price": str(current) if current is not None else None,
            "change_percent": str(change) if change is not None else None, "relation": relation,
            "quote_date": (quote or {}).get("as_of"), "quote_status": status,
            "amount": str(amount) if amount is not None else None, "amount_estimated": amount_estimated}


def effective_price(row, estimates):
    reported = row.get("reported_price")
    if reported is not None:
        return reported, False, row.get("price_range_low"), row.get("price_range_high")
    estimate = estimates.get(((row.get("ticker") or "").replace(".", "-"), row.get("transaction_date")))
    return (estimate[0], True, estimate[1], estimate[2]) if estimate else (None, False, None, None)


def feed_prices(s, revision_ids):
    """Price comparisons for exactly the immutable revisions visible on screen."""
    from iirp.analysis.insider_windows import provider_symbol
    from iirp.insider.views import _trader_key
    from iirp.market.stock_quotes import stock_quote_views
    from iirp.models import FeedRevision, TransactionEvent

    # A group may contain more than the twenty compact transaction rows. Read its
    # immutable facts, so its trader average never silently omits those rows.
    revisions = list(s.scalars(select(FeedRevision).where(FeedRevision.id.in_(revision_ids))))
    rows = [row for revision in revisions
            for row in revision.data.get("transactions", [])]
    tickers = {symbol for row in rows if (symbol := provider_symbol(row.get("ticker")))}
    quotes = {item["symbol"]: item for item in stock_quote_views(s, sorted(tickers))}
    facts = {event.id: event for event in s.scalars(select(TransactionEvent).where(TransactionEvent.id.in_([row["id"] for row in rows])))}
    estimates = estimate_prices(s, [(row.get("ticker"), facts[row["id"]].transaction_date)
                                   for row in rows if row["id"] in facts and facts[row["id"]].reported_price is None])
    buckets = {}
    for revision, row in [(revision, row) for revision in revisions for row in revision.data.get("transactions", [])]:
        event = facts.get(row["id"])
        if event is None:
            continue
        bucket = buckets.setdefault(revision.id + ":" + _trader_key(row), [])
        data = {"ticker": row.get("ticker"), "transaction_date": event.transaction_date,
                "reported_price": event.reported_price, "price_range_low": event.price_range_low,
                "price_range_high": event.price_range_high}
        price, estimated, low, high = effective_price(data, estimates)
        shares = event.trade_shares
        eligible = row.get("status") == "CURRENT" and not row.get("is_amendment_update") and not row.get("date_anomaly")
        bucket.append((price, shares, estimated, low, high, data["ticker"], eligible))
    output = {}
    for key, trades in buckets.items():
        known = [(price, shares) for price, shares, *_ in trades if price is not None and shares is not None]
        shares = sum((quantity for _, quantity in known), Decimal(0))
        known_value = sum((price * quantity for price, quantity in known), Decimal(0)) if known else None
        price = known_value / shares if known_value is not None and shares > 0 else trades[0][0] if len(trades) == 1 else None
        eligible = [(item[0], item[1]) for item in trades if item[0] is not None and item[1] is not None and item[6]]
        amount = sum((price * quantity for price, quantity in eligible), Decimal(0)) if eligible else None
        estimated = any(item[2] for item in trades)
        lows = [item[3] for item in trades if item[3] is not None]
        highs = [item[4] for item in trades if item[4] is not None]
        quote = quotes.get((trades[0][5] or "").replace(".", "-"))
        output[key] = price_comparison(price, quote, estimated=estimated,
                                       low=min(lows) if lows else None, high=max(highs) if highs else None,
                                       amount=amount, amount_estimated=estimated)
    return {"items": output}
