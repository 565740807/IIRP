"""Explicit benchmark identities and their prices from the 24-hour cache."""

import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import object_session

from iirp.market.cache import current_cache, price_bars
from iirp.messages import UserError, msg
from iirp.models import Security

INDEXES = {"^GSPC": msg("market.name.GSPC"), "^IXIC": msg("market.name.IXIC")}


def validate_benchmark(symbol):
    if not symbol:
        return None
    symbol = symbol.strip().upper()
    if symbol not in INDEXES and not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,19}", symbol):
        raise UserError("benchmark.invalid")
    return symbol


def benchmark_snapshot(params, stock, s=None):
    symbol = params.get("benchmark")
    if not symbol:
        return None
    s = s or object_session(stock)
    security = s.scalar(select(Security).where(Security.symbol == symbol).order_by(Security.id).limit(1)) if s else None
    output = {"symbol": symbol, "name": INDEXES.get(symbol, symbol), "status": "benchmark_identity_pending",
              "security_id": security.id if security else None, "dataset_id": None, "price_basis": "split_only",
              "calendar": security.calendar if security else None, "listing_date": None}
    if not security:
        return output
    if security.instrument == "ETF":
        output["name"] = security.name or symbol
    if security.metadata_json.get("firstTradeDateEpochUtc"):
        output["listing_date"] = datetime.fromtimestamp(float(security.metadata_json["firstTradeDateEpochUtc"]), timezone.utc).date().isoformat()
    if security.status != "VERIFIED":
        output["status"] = "benchmark_identity_pending" if security.status in {"PENDING", "VERIFIED_MARKET"} else "benchmark_identity_unconfirmed"
        return output
    if security.instrument != ("INDEX" if symbol in INDEXES else "ETF"):
        output["status"] = "benchmark_not_etf_or_index"
        return output
    if security.calendar != stock.calendar or security.currency != stock.currency:
        output["status"] = "benchmark_calendar_or_currency_mismatch"
        return output
    dataset = current_cache(s, security.id)
    if dataset:
        output.update(status="available", dataset_id=dataset.id)
    else:
        output["status"] = "benchmark_prices_pending"
    return output


def benchmark_data(s, snapshot, *, ranges=None):
    if not snapshot:
        return None
    return {**snapshot, "bars": price_bars(s, snapshot["security_id"], snapshot["dataset_id"], ranges=ranges)[0] if snapshot.get("dataset_id") else []}


def plan_benchmark(s, scope, request, start, end):
    """Attach to the user's stock scope: controls, leases and sharing apply.

    Returns a truthful incomplete reason without withholding usable stock data.
    """
    symbol = request.params.get("benchmark")
    if not symbol:
        return None
    from iirp.jobs.batches import add_job, advisory
    from iirp.market.cache import ensure_prices, fetch_state
    advisory(s, ["security", symbol])
    security = s.scalar(select(Security).where(Security.symbol == symbol).order_by(Security.id).limit(1))
    if not security:
        security = Security(symbol=symbol)
        s.add(security)
        s.flush()
    if security.status in {"PENDING", "VERIFIED_MARKET"}:
        job = add_job(s, scope, "market_identity", {"symbol": symbol, "security_id": security.id, "benchmark_contract": 1})
        job.title = msg("job.title.benchmark_identity", name=INDEXES.get(symbol, symbol))
        return job.error or msg("benchmark.identity_pending")
    snapshot = benchmark_snapshot(request.params, s.get(Security, scope.security_id), s)
    if snapshot["status"] not in {"available", "benchmark_prices_pending"}:
        return msg("benchmark.mismatch")
    ensured = ensure_prices(s, scope, security, start, end,
                            title=msg("job.title.benchmark_prices", name=INDEXES.get(symbol, symbol)))
    status, reason = fetch_state(ensured)
    if status == "READY":
        return None
    return reason or msg("benchmark.prices_pending")
