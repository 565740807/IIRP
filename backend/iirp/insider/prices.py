"""Insider display prices from reported facts and the existing expiring daily cache.

Every displayed price is checked against a reference: the close of the trade
day in the 24-hour daily cache, otherwise the latest stock quote. A missing or
non-positive price (option exercises, awards and other $0 rows) has nothing to
check and is "not_applicable". When no reference exists the price is
"unchecked" and shown as is. A reported price whose ratio
max(price / reference, reference / price) reaches the configured limit is
"mismatch": it stays visible as reported, and the overview leaves it out of
amount rankings and totals.

Numbers leave the backend rounded for display: prices to 2 decimals (4 below
$1), amounts and percentages to 2 decimals, share counts without trailing zeros.
"""

import re
import tomllib
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache

from sqlalchemy import select

from iirp.analysis.insider_windows import provider_symbol
from iirp.config import ROOT
from iirp.insider.tickers import normalized_ticker
from iirp.insider.typed import decimal_value
from iirp.models import PriceCache, PriceCacheBar, Security, now

CENT = Decimal("0.01")
SUB_DOLLAR = Decimal("0.0001")
QUOTE_STATUS = {"LIVE": "live", "CLOSED": "closed", "STALE": "delayed", "NOT_FETCHED": "missing"}


@lru_cache
def overview_settings() -> dict:
    """List sizes and the price check ratio, from config/analysis-defaults.toml."""
    with (ROOT / "config/analysis-defaults.toml").open("rb") as source:
        return tomllib.load(source)["insider_overview"]


def _fixed(value, step):
    value = decimal_value(value)
    return None if value is None else format(value.quantize(step, ROUND_HALF_UP), "f")


def price_text(value):
    value = decimal_value(value)
    return None if value is None else _fixed(value, CENT if abs(value) >= 1 else SUB_DOLLAR)


def money_text(value):
    return _fixed(value, CENT)


def percent_text(value):
    return _fixed(value, CENT)


def shares_text(value):
    value = decimal_value(value)
    if value is None:
        return None
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


@lru_cache(maxsize=8192)
def display_ticker(*texts):
    """First usable symbol of the given ticker texts, in order (issuer's first).

    A filing may state several classes ("LEN, LEN.B"); the first valid one is
    used for display and quotes. Without any, the first text that is not a
    placeholder such as "NONE" is shown as written.
    """
    for text in texts:
        for token in re.split(r"[,;\s]+", text or ""):
            if normalized_ticker(token) and provider_symbol(token):
                return token.strip().upper()
    return next(filter(None, map(normalized_ticker, texts)), None)


def quote_symbol(ticker):
    """Yahoo form of a display ticker (BRK.B → BRK-B); None when unusable."""
    return provider_symbol(ticker)


def daily_bars(s, keys):
    """(high, low, close) per (symbol, day) from live, valid 24-hour cache bars."""
    keys = {(symbol.replace(".", "-"), day) for symbol, day in keys if symbol and day}
    if not keys:
        return {}
    days = [day for _, day in keys]
    rows = s.execute(
        select(Security.symbol, PriceCacheBar.session_date,
               PriceCacheBar.high, PriceCacheBar.low, PriceCacheBar.close)
        .join(PriceCache, PriceCache.security_id == Security.id)
        .join(PriceCacheBar, PriceCacheBar.cache_id == PriceCache.id)
        .where(PriceCache.expires_at > now(), PriceCacheBar.status == "VALID",
               Security.symbol.in_(sorted({symbol for symbol, _ in keys})),
               PriceCacheBar.session_date.between(min(days), max(days)))
        .order_by(Security.id)
    ).all()
    result = {}
    for symbol, day, high, low, close in rows:
        if (symbol, day) in keys:
            result.setdefault((symbol, day), (high, low, close))
    return result


def estimate_prices(s, keys, bars=None):
    """Typical price (high + low + close) / 3 with the day's range, for complete bars."""
    bars = daily_bars(s, keys) if bars is None else bars
    result = {}
    for key, (high, low, close) in bars.items():
        if all(value is not None for value in (high, low, close)) and 0 <= low <= close <= high:
            result[key] = ((high + low + close) / Decimal(3), low, high)
    return result


def mismatch_ratio_text():
    """The configured mismatch ratio as display text, e.g. "10"."""
    value = Decimal(str(overview_settings()["price_mismatch_ratio"]))
    return format(value.normalize(), "f")


def price_check(price, bar=None, quote=None, *, limit=None):
    """Compare a price with the trade day's cached close, else the latest quote."""
    limit = Decimal(str(overview_settings()["price_mismatch_ratio"] if limit is None else limit))
    price = decimal_value(price)
    if price is None or price <= 0:
        return {"status": "not_applicable", "ratio": None}
    close = decimal_value(bar[2]) if bar else None
    current = decimal_value((quote or {}).get("value"))
    reference = close if close is not None and close > 0 else (
        current if current is not None and current > 0 else None)
    if reference is None:
        return {"status": "unchecked", "ratio": None}
    ratio = max(price / reference, reference / price)
    return {"status": "mismatch" if ratio >= limit else "ok", "ratio": ratio}


def combined_check(checks):
    """One trader's line: any mismatch wins, then any unchecked price.

    Prices without anything to check are ignored; a line made only of them is
    "not_applicable".
    """
    checks = [check for check in checks if check["status"] != "not_applicable"]
    if not checks:
        return {"status": "not_applicable", "ratio": None}
    mismatched = [check["ratio"] for check in checks if check["status"] == "mismatch"]
    if mismatched:
        return {"status": "mismatch", "ratio": max(mismatched)}
    if any(check["status"] == "unchecked" for check in checks):
        return {"status": "unchecked", "ratio": None}
    return {"status": "ok", "ratio": None}


def price_comparison(price, quote=None, *, estimated=False, low=None, high=None, amount=None,
                     amount_estimated=False, check=None):
    current = decimal_value((quote or {}).get("value"))
    price = decimal_value(price)
    change = relation = None
    if current is not None and price is not None and price > 0:
        change = (current / price - 1) * 100
        relation = "near" if abs(change) <= 1 else "higher" if change > 0 else "lower"
    check = check or price_check(price)
    mismatch = check["status"] == "mismatch"
    return {"price": price_text(price), "estimated": estimated,
            "range_low": price_text(low), "range_high": price_text(high),
            "current_price": price_text(current),
            "change_percent": percent_text(change), "relation": relation,
            "quote_date": (quote or {}).get("as_of"),
            "quote_status": QUOTE_STATUS.get((quote or {}).get("status"), "missing"),
            "amount": money_text(amount), "amount_estimated": amount_estimated,
            "price_check": check["status"],
            "mismatch_ratio": _fixed(check["ratio"], Decimal(1)) if mismatch else None}


def effective_price(row, estimates):
    reported = row.get("reported_price")
    if reported is not None:
        return reported, False, row.get("price_range_low"), row.get("price_range_high")
    symbol = (row.get("ticker") or "").replace(".", "-")
    estimate = estimates.get((symbol, row.get("transaction_date")))
    return (estimate[0], True, estimate[1], estimate[2]) if estimate else (None, False, None, None)


def feed_prices(s, revision_ids):
    """Price comparisons for exactly the immutable revisions visible on screen."""
    from iirp.insider.views import _trader_key
    from iirp.market.stock_quotes import stock_quote_views
    from iirp.models import FeedRevision, Issuer, TransactionEvent

    # A group may contain more than the twenty compact transaction rows. Read its
    # immutable facts, so its trader average never silently omits those rows.
    revisions = list(s.scalars(select(FeedRevision).where(FeedRevision.id.in_(revision_ids))))
    issuers = dict(s.execute(select(Issuer.id, Issuer.ticker).where(
        Issuer.id.in_({revision.issuer_id for revision in revisions}))).all())
    pairs = [(revision, row) for revision in revisions
             for row in revision.data.get("transactions", [])]
    tickers = {id(row): display_ticker(issuers.get(revision.issuer_id), row.get("ticker"))
               for revision, row in pairs}
    symbols = {symbol for ticker in tickers.values() if (symbol := quote_symbol(ticker))}
    quotes = {item["symbol"]: item for item in stock_quote_views(s, sorted(symbols))}
    facts = {event.id: event for event in s.scalars(
        select(TransactionEvent).where(TransactionEvent.id.in_([row["id"] for _, row in pairs])))}
    bars = daily_bars(s, [(tickers[id(row)], facts[row["id"]].transaction_date)
                          for _, row in pairs if row["id"] in facts])
    estimates = estimate_prices(s, (), bars)
    buckets = {}
    for revision, row in pairs:
        event = facts.get(row["id"])
        if event is None:
            continue
        bucket = buckets.setdefault(revision.id + ":" + _trader_key(row), [])
        ticker = tickers[id(row)]
        data = {"ticker": ticker, "transaction_date": event.transaction_date,
                "reported_price": event.reported_price, "price_range_low": event.price_range_low,
                "price_range_high": event.price_range_high}
        price, estimated, low, high = effective_price(data, estimates)
        symbol = quote_symbol(ticker)
        check = price_check(price, bars.get((symbol, event.transaction_date)), quotes.get(symbol))
        eligible = (row.get("status") == "CURRENT" and not row.get("is_amendment_update")
                    and not row.get("date_anomaly"))
        bucket.append((price, event.trade_shares, estimated, low, high, symbol, eligible, check))
    output = {}
    for key, trades in buckets.items():
        known = [(price, shares) for price, shares, *_ in trades
                 if price is not None and shares is not None]
        shares = sum((quantity for _, quantity in known), Decimal(0))
        known_value = (sum((price * quantity for price, quantity in known), Decimal(0))
                       if known else None)
        if known_value is not None and shares > 0:
            price = known_value / shares
        else:
            price = trades[0][0] if len(trades) == 1 else None
        eligible = [(item[0], item[1]) for item in trades
                    if item[0] is not None and item[1] is not None and item[6]]
        amount = (sum((price * quantity for price, quantity in eligible), Decimal(0))
                  if eligible else None)
        estimated = any(item[2] for item in trades)
        lows = [item[3] for item in trades if item[3] is not None]
        highs = [item[4] for item in trades if item[4] is not None]
        output[key] = price_comparison(price, quotes.get(trades[0][5]), estimated=estimated,
                                       low=min(lows) if lows else None,
                                       high=max(highs) if highs else None,
                                       amount=amount, amount_estimated=estimated,
                                       check=combined_check(item[7] for item in trades))
    return {"items": output, "price_mismatch_ratio": mismatch_ratio_text()}
