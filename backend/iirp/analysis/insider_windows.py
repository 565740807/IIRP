"""Price change n sessions before and after insider trades (D16), for lists.

With t the trade's session (the next session when the trade date is not one)
and C the close: before = C(t)/C(t−n) − 1, after = C(t+n)/C(t) − 1. A session
that has not happened yet stays empty with its expected date; today's session
before the close is marked intraday. Prices come from the 24-hour cache of
the ticker the filing states (D14); nothing here requests a provider.

The ticker is the one written in the filing. Its security counts as
confirmed only when SEC's ticker list linked it to this issuer; otherwise the
values are shown and marked as pending confirmation.
"""

import re
from bisect import bisect_left
from datetime import date, timedelta

from sqlalchemy import select

from iirp.analysis.calendar import ET, completed_through, sessions
from iirp.analysis.research import _bars_by_date, _price, _ratio, _text
from iirp.messages import msg
from iirp.models import ACTIVE, Job, PriceCache, Security, now

TICKER = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,9}")
# Default company/person range (D15); a fetch for any list covers it too, so
# opening the company page afterwards needs no second request.
COVER_DAYS = 183
FETCH_TICKERS = 20


def provider_symbol(ticker: str | None) -> str | None:
    """Filing ticker as Yahoo writes it (BRK.B → BRK-B); None when unusable."""
    value = (ticker or "").strip().upper()
    if not TICKER.fullmatch(value) or value in {"NONE", "NA", "N/A", "NULL"}:
        return None
    return value.replace(".", "-")


def _today():
    return now().astimezone(ET).date()


def _securities(s, symbols):
    if not symbols:
        return {}
    rows = s.scalars(select(Security).where(Security.symbol.in_(symbols)).order_by(Security.id)).all()
    found = {}
    for row in rows:
        found.setdefault(row.symbol, row)
    return found


def _ticker_state(s, security, issuer_ids):
    """Whether prices are ready, being fetched, not fetched, or not available."""
    if security is None:
        return {"status": "not_fetched"}
    base = {"security_id": security.id,
            "confirmed": bool(security.issuer_id) and security.issuer_id in issuer_ids}
    if security.status not in {"PENDING", "VERIFIED"}:
        return {**base, "status": "unavailable", "reason": msg("window.reason.not_us_stock")}
    active = s.scalar(select(Job.id).where(
        Job.kind.in_(("market_identity", "market_history")), Job.status.in_(ACTIVE),
        Job.target["security_id"].astext == security.id).limit(1))
    cache = s.scalar(select(PriceCache).where(PriceCache.security_id == security.id,
                                              PriceCache.expires_at > now()))
    if active:
        return {**base, "status": "fetching", "cache": cache}
    if security.status == "PENDING":
        failed = s.scalar(select(Job.id).where(
            Job.kind == "market_identity", Job.status.in_(("FAILED", "PARTIAL")),
            Job.target["security_id"].astext == security.id).limit(1))
        if failed:
            return {**base, "status": "unavailable", "reason": msg("window.reason.no_quote")}
        return {**base, "status": "not_fetched"}
    if cache is None:
        failed_today = s.scalar(select(Job.id).where(
            Job.kind == "market_history", Job.status.in_(("FAILED", "PARTIAL")),
            Job.target["security_id"].astext == security.id,
            Job.target["round"].astext == _today().isoformat()).limit(1))
        if failed_today:
            return {**base, "status": "unavailable", "reason": msg("window.reason.no_prices")}
        return {**base, "status": "not_fetched"}
    return {**base, "status": "ready", "cache": cache}


class _Sessions:
    """Exchange sessions around a set of dates, with t−n and t+n lookups."""

    def __init__(self, first: date, last: date, n: int, calendar: str):
        pad = timedelta(days=n * 2 + 14)
        self.days = sessions(first - pad, last + pad, calendar)

    def anchor(self, day: date) -> int:
        return bisect_left(self.days, day)

    def at(self, index: int) -> date | None:
        return self.days[index] if 0 <= index < len(self.days) else None


def _change(index, start, end, cutoff, today_session):
    """One before/after change and why it may be empty."""
    if start is None or end is None:
        return {"value": None, "start_date": None, "end_date": None, "status": "missing"}
    later = max(start, end)
    result = {"start_date": start.isoformat(), "end_date": end.isoformat(), "value": None}
    if later > cutoff:
        result["status"] = "intraday" if later == today_session else "pending"
        return result
    first, last = _price(index, start, cutoff), _price(index, end, cutoff)
    if first is None or last is None:
        result["status"] = "missing"
        return result
    result["value"] = _ratio(first, last)
    result["status"] = "available"
    return result


def trade_windows(s, items: list[dict], n: int, *, today: date | None = None) -> dict:
    """Before/after-n changes for ``items`` of {ticker, date, issuer_id}."""
    from iirp.market.cache import price_bars

    by_symbol: dict[str, list[dict]] = {}
    for item in items:
        symbol = provider_symbol(item.get("ticker"))
        item["_symbol"] = symbol
        # A date far in the future is a filing error (D19); nothing to compute.
        if symbol and item.get("date") and item["date"][:10] <= (_today() + timedelta(days=31)).isoformat():
            by_symbol.setdefault(symbol, []).append(item)
    securities = _securities(s, list(by_symbol))
    tickers, results = {}, {}
    current = today or _today()
    for symbol, rows in by_symbol.items():
        security = securities.get(symbol)
        issuers = {row.get("issuer_id") for row in rows if row.get("issuer_id")}
        state = _ticker_state(s, security, issuers)
        cache = state.pop("cache", None)
        calendar = (security.calendar if security else None) or "XNYS"
        days = [date.fromisoformat(row["date"][:10]) for row in rows]
        grid = _Sessions(min(days), max(days), n, calendar)
        cutoff = completed_through(today, calendar)
        today_session = current if sessions(current, current, calendar) else None
        bars, _ = price_bars(s, security.id, cache.id) if cache else ([], None)
        index = _bars_by_date(bars)
        needed_start, needed_end = None, None
        for row, day in zip(rows, days, strict=True):
            position = grid.anchor(day)
            anchor = grid.at(position)
            before, after = grid.at(position - n), grid.at(position + n)
            needed_start = before if needed_start is None or (before and before < needed_start) else needed_start
            closed = [day for day in (anchor, after) if day and day <= cutoff]
            if closed and (needed_end is None or max(closed) > needed_end):
                needed_end = max(closed)
            entry = {"anchor_date": anchor.isoformat() if anchor else None}
            if cache is None:
                for side, start, end in (("before", before, anchor), ("after", anchor, after)):
                    later = max(start, end) if start and end else None
                    entry[side] = {"value": None, "start_date": _text(start), "end_date": _text(end),
                                   "status": ("intraday" if later == today_session else "pending")
                                   if later and later > cutoff else "no_prices"}
            else:
                entry["before"] = _change(index, before, anchor, cutoff, today_session)
                entry["after"] = _change(index, anchor, after, cutoff, today_session)
            results[(symbol, row["date"][:10])] = entry
        if state["status"] == "ready" and (
                (needed_start and cache.start_date > needed_start)
                or (needed_end and cache.complete_through < needed_end)):
            # The cache misses earlier dates or sessions closed since; one wider fetch is due.
            state["status"] = "not_fetched"
        if cache is not None:
            state["fetched_at"] = cache.fetched_at.isoformat()
            state["expires_at"] = cache.expires_at.isoformat()
        state["needed_start"] = _text(needed_start)
        tickers[symbol] = state
    output = []
    for item in items:
        symbol = item.pop("_symbol")
        key = (symbol, (item.get("date") or "")[:10])
        output.append({"ticker": item.get("ticker"), "date": item.get("date"), "symbol": symbol,
                       **results.get(key, {"anchor_date": None, "before": None, "after": None})})
    return {"n": n, "items": output, "tickers": tickers}


def request_trade_prices(s, items: list[dict], n: int) -> dict:
    """Plan one price fetch per ticker whose windows still lack prices.

    Fetches cover the default six-month view and the earliest t−n needed,
    through today, as one request per ticker (D14). Asking again while a fetch
    runs, after it failed today, or once the cache covers the need adds nothing.
    """
    from iirp.jobs.batches import _create
    from iirp.market.yahoo import digest

    windows = trade_windows(s, items, n)
    due = sorted(symbol for symbol, state in windows["tickers"].items() if state["status"] == "not_fetched")
    if not due:
        return {"requested": [], "batch_ids": []}
    today = _today()
    starts = [date.fromisoformat(windows["tickers"][symbol]["needed_start"])
              for symbol in due if windows["tickers"][symbol].get("needed_start")]
    start = min([today - timedelta(days=COVER_DAYS), *starts])
    batch_ids = []
    for offset in range(0, len(due), FETCH_TICKERS):
        chunk = due[offset:offset + FETCH_TICKERS]
        params = {"kind": "market_history", "tickers": chunk, "start_date": start.isoformat(),
                  "end_date": today.isoformat(), "purpose": "insider_window"}
        params["request_id"] = "insider-prices-" + digest([params, today.isoformat()])[:40]
        batch, _ = _create(s, params, trigger="automatic", policy_key="market")
        batch_ids.append(batch.id)
    return {"requested": due, "batch_ids": batch_ids}
