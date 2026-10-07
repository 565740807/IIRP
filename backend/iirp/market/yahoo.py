"""Explicit Yahoo adapter and bar validation. Network never runs in a DB transaction.

Fetched daily prices live in the 24-hour cache (``iirp.market.cache``)."""

import hashlib
import json
import math
import time
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from importlib.metadata import version

from sqlalchemy import select

from iirp.config import ROOT, settings
from iirp.messages import UserError, msg
from iirp.models import SecurityIdentifier, now

MARKETS = {
    "^GSPC": msg("market.name.GSPC"),
    "^IXIC": msg("market.name.IXIC"),
    "^DJI": msg("market.name.DJI"),
    "GC=F": msg("market.name.GC"),
    "^VIX": "VIX",
}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()
    ).hexdigest()


def number(value):
    if value is None:
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def plain(value):
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(x) for x in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):
        return plain(value.item())
    if hasattr(value, "to_dict"):
        return plain(value.to_dict())
    return value


def source_contract():
    path = ROOT / "config/provider-contracts.json"
    if not path.exists():
        return {"verified": False, "reason": msg("market.contract_unverified")}
    contract = json.loads(path.read_text())["yfinance"]
    return {
        **contract,
        "verified": bool(
            contract.get("verified") and contract.get("library_version") == version("yfinance")
        ),
    }


def fetch_market(kind, target):
    import yfinance as yf

    yf.set_tz_cache_location(str(settings().runtime_dir / "provider-cache"))
    symbol = target["symbol"]
    from iirp.market.http import timed_session

    timing = timed_session()
    timing.reset()
    adapter_started = time.perf_counter()
    ticker = yf.Ticker(symbol, session=timing.session)
    if kind == "market_identity":
        info = ticker.get_info()
        keys = (
            "symbol",
            "shortName",
            "longName",
            "quoteType",
            "currency",
            "exchange",
            "fullExchangeName",
            "timeZoneFullName",
            "firstTradeDateEpochUtc",
            "lastFiscalYearEnd",
            "nextFiscalYearEnd",
            "mostRecentQuarter",
        )
        metadata = {key: plain(info.get(key)) for key in keys}
        if (
            not metadata.get("symbol")
            or not metadata.get("quoteType")
            or not metadata.get("currency")
        ):
            raise UserError("market.identity_fields_missing")
        return {
            "metadata": metadata,
            "symbol": symbol,
            "fetched_at": now().isoformat(),
            "library_version": version("yfinance"),
            "timing": {**timing.snapshot(), "adapter_seconds": time.perf_counter() - adapter_started,
                       "adapter_calls": 1},
        }
    if kind == "market_quote":
        # Homepage snapshots need the current provider quote, independently of the
        # frozen completed-session cutoff used by research. Include one following
        # date for a futures session that has already opened across midnight.
        observed_day = now().date()
        start = observed_day - timedelta(days=40)
        end = observed_day + timedelta(days=1)
    else:
        start = date.fromisoformat(target["start_date"])
        end = date.fromisoformat(target["end_date"])
    parameters = dict(
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        interval="1d",
        actions=True,
        auto_adjust=False,
        back_adjust=False,
        repair=False,
        keepna=True,
        rounding=False,
        prepost=False,
        timeout=12,
        raise_errors=True,
    )
    frame = ticker.history(**parameters)
    adapter_seconds = time.perf_counter() - adapter_started
    # HistoryMetadata is a lazy Mapping. Iterating all keys can materialize
    # tradingPeriods and trigger an unrelated intraday request behind our budget.
    stable_keys = (
        "currency",
        "symbol",
        "exchangeName",
        "fullExchangeName",
        "instrumentType",
        "firstTradeDate",
        "regularMarketTime",
        "timezone",
        "exchangeTimezoneName",
        "regularMarketPrice",
        "chartPreviousClose",
        "currentTradingPeriod",
    )
    raw_metadata = ticker.history_metadata if not frame.empty else None
    metadata = (
        {key: plain(raw_metadata.get(key)) for key in stable_keys}
        if raw_metadata is not None
        else {}
    )
    records = []
    for stamp, row in frame.iterrows():
        values = {"date": stamp.date().isoformat()}
        for source, dest in [
            ("Open", "open"),
            ("High", "high"),
            ("Low", "low"),
            ("Close", "close"),
            ("Adj Close", "adj_close"),
            ("Volume", "volume"),
            ("Dividends", "dividends"),
            ("Stock Splits", "splits"),
        ]:
            val = number(row.get(source))
            values[dest] = str(val) if val is not None else None
        records.append(values)
    return {
        "provider": "yfinance",
        "library_version": version("yfinance"),
        "symbol": symbol,
        "parameters": parameters,
        "records": records,
        "metadata": metadata,
        "fetched_at": now().isoformat(),
        "contract": source_contract(),
        "source_kind": "adapter_records",
        # The publisher additionally checks complete exchange-session coverage.
        # Missing action columns are unknown, never an implicit zero.
        "actions_complete": "Stock Splits" in frame.columns,
        "timing": {**timing.snapshot(), "adapter_seconds": adapter_seconds, "adapter_calls": 1,
                   "measurement": "HTTP client elapsed includes connection and transfer; adapter includes library work"},
    }


def resolve_metadata(s, security, response):
    info = response["metadata"]
    quote_type = info.get("quoteType", "UNKNOWN")
    exchange = info.get("exchange")
    # Do not confuse index/futures sessions with the stock calendar.
    calendars = {
        "NMS": "XNYS",
        "NGM": "XNYS",
        "NCM": "XNYS",
        "NYQ": "XNYS",
        "ASE": "XNYS",
        "PCX": "XNYS",
        "BTS": "XNYS",
        "BATS": "XNYS",
    }
    security.name = info.get("longName") or info.get("shortName") or security.symbol
    security.instrument = quote_type
    security.currency = info.get("currency")
    security.exchange = exchange
    security.calendar = calendars.get(exchange) if quote_type in ("EQUITY", "ETF") else None
    # Quote refreshes cannot erase separately verified SEC identity/fiscal evidence.
    security.metadata_json = {**security.metadata_json, **info}
    security.status = (
        "VERIFIED" if security.calendar and security.currency == "USD" else "NEEDS_REVIEW"
    )
    if security.symbol in MARKETS:
        security.status = "VERIFIED_MARKET"
    if security.symbol in {"^GSPC", "^IXIC"} and info.get("symbol") == security.symbol and quote_type == "INDEX" and security.currency == "USD":
        # These US equity price indexes use the same daily close sessions as
        # the supported US stocks. Other indexes/futures keep quote-only status.
        security.calendar, security.status = "XNYS", "VERIFIED"
    identifier = s.scalar(
        select(SecurityIdentifier).where(SecurityIdentifier.security_id == security.id)
    )
    if not identifier:
        # This mapping is observed now; it is not proof of historic ticker ownership.
        s.add(
            SecurityIdentifier(
                security_id=security.id,
                provider="yfinance",
                symbol=info.get("symbol") or security.symbol,
                valid_from=now().date(),
            )
        )


def validate_bar(row, security, completed):
    day = date.fromisoformat(row["date"])
    values = {
        k: number(row.get(k)) for k in ("open", "high", "low", "close", "adj_close", "volume")
    }
    if day > completed:
        return values, "UNCONFIRMED", msg("market.bar.unconfirmed")
    o, h, low, c = (values[k] for k in ("open", "high", "low", "close"))
    if any(x is None for x in (o, h, low, c)):
        return values, "MISSING_FIELDS", msg("market.bar.missing_fields")
    if security.instrument in ("EQUITY", "ETF") and min(o, h, low, c) <= 0:
        return values, "INVALID", msg("market.bar.not_positive")
    if (
        h < max(o, low, c)
        or low > min(o, h, c)
        or (values["volume"] is not None and values["volume"] < 0)
    ):
        return values, "INVALID", msg("market.bar.inconsistent")
    return values, "VALID", None


def price_identity_conflicts(s, security, response, provider):
    """Only explicit contradictions reject a response; absent metadata is unknown.

    Aliases must come from this security's observed provider mapping. Case and
    whitespace are presentation differences; punctuation or another share class
    is never guessed to identify the same security.
    """
    def normalize(value):
        return str(value).strip().upper() if value is not None else ""

    aliases = {normalize(security.symbol), normalize(security.metadata_json.get("symbol"))}
    aliases.update(normalize(symbol) for symbol in s.scalars(
        select(SecurityIdentifier.symbol).where(
            SecurityIdentifier.security_id == security.id,
            SecurityIdentifier.provider == provider,
            SecurityIdentifier.valid_from <= now().date(),
            (SecurityIdentifier.valid_to.is_(None)) | (SecurityIdentifier.valid_to >= now().date()),
        )
    ))
    metadata = response.get("metadata") or {}
    conflicts = []
    for field, value in (("symbol", response.get("symbol")), ("symbol", metadata.get("symbol")),
                         ("currency", metadata.get("currency")),
                         ("instrument", metadata.get("instrumentType")),
                         ("instrument", metadata.get("quoteType"))):
        # Currency units can be case-sensitive (GBp versus GBP); do not
        # normalize pence into pounds. Symbols and instrument enums are not.
        actual = str(value).strip() if field == "currency" and value is not None else normalize(value)
        stored = getattr(security, field)
        expected = str(stored).strip() if field == "currency" and stored is not None else normalize(stored)
        if not actual or not expected or (field == "instrument" and "UNKNOWN" in (actual, expected)):
            continue
        mismatch = actual not in aliases if field == "symbol" else actual != expected
        if mismatch:
            conflicts.append(msg("market.identity_conflict", field=field, actual=actual, expected=expected))
    return conflicts


def quote_from_history(symbol, response):
    from iirp.market.quotes import quote_from_snapshot

    return quote_from_snapshot(symbol, response)
