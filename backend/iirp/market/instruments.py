"""Instruments of the home market strip: names, units, how a change is shown, and when each appears.

The catalog is the only list of strip instruments; the market detail page accepts the
same symbols. Which instruments the strip shows is a display setting
(``iirp.display``); by default every instrument except bitcoin. Index futures (ES=F,
NQ=F) appear only outside the US regular stock session and only while the futures
themselves trade; during the regular session they are neither shown nor fetched.
Each instrument's own session comes from its quote (Yahoo's trading period for that
instrument), never from the stock calendar, so futures, the dollar index and bitcoin
refresh on their own hours (config/refresh.toml).
"""

from dataclasses import dataclass
from datetime import datetime

from iirp.analysis.calendar import ET, session_bounds, sessions
from iirp.messages import msg


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str  # message code
    # index | yield | currency_index | futures | crypto
    kind: str
    unit: str  # message code
    instrument: str  # message code describing the series
    default: bool = True
    # Index futures: only outside the US regular session, while the futures trade.
    off_hours_only: bool = False
    # US stock indexes: the change must be against the previous NYSE session's close.
    stock_calendar: bool = False
    # Shorter name for a strip tile (message code), when the full name does not fit.
    short: str | None = None


INSTRUMENTS = (
    Instrument("^GSPC", "market.name.GSPC", "index", "quote.unit.index_points", "quote.instrument.index", stock_calendar=True),
    Instrument("^IXIC", "market.name.IXIC", "index", "quote.unit.index_points", "quote.instrument.index", stock_calendar=True,
               short="market.short.IXIC"),
    Instrument("^NDX", "market.name.NDX", "index", "quote.unit.index_points", "quote.instrument.index", stock_calendar=True),
    Instrument("^DJI", "market.name.DJI", "index", "quote.unit.index_points", "quote.instrument.index", stock_calendar=True),
    Instrument("^RUT", "market.name.RUT", "index", "quote.unit.index_points", "quote.instrument.index", stock_calendar=True),
    Instrument("^VIX", "market.name.VIX", "index", "quote.unit.index_points", "quote.instrument.volatility", stock_calendar=True),
    Instrument("^TNX", "market.name.TNX", "yield", "quote.unit.percent_yield", "quote.instrument.yield"),
    Instrument("DX-Y.NYB", "market.name.DXY", "currency_index", "quote.unit.index_points", "quote.instrument.dollar_index"),
    Instrument("GC=F", "market.name.GC", "futures", "quote.unit.usd_per_ounce", "quote.instrument.gold_futures"),
    Instrument("CL=F", "market.name.CL", "futures", "quote.unit.usd_per_barrel", "quote.instrument.oil_futures"),
    Instrument("ES=F", "market.name.ES", "futures", "quote.unit.index_points", "quote.instrument.index_futures", off_hours_only=True),
    Instrument("NQ=F", "market.name.NQ", "futures", "quote.unit.index_points", "quote.instrument.index_futures", off_hours_only=True),
    Instrument("BTC-USD", "market.name.BTC", "crypto", "quote.unit.usd", "quote.instrument.crypto", default=False),
)
BY_SYMBOL = {item.symbol: item for item in INSTRUMENTS}
DEFAULT_SYMBOLS = [item.symbol for item in INSTRUMENTS if item.default]


def names():
    """Symbol → name message, in strip order."""
    return {item.symbol: msg(item.name) for item in INSTRUMENTS}


def ordered(symbols):
    """Known symbols in catalog order, without duplicates."""
    wanted = set(symbols)
    return [item.symbol for item in INSTRUMENTS if item.symbol in wanted]


def us_regular_session(stamp: datetime) -> bool:
    """True inside the NYSE regular session (09:30 to the actual close, early closes included)."""
    local = stamp.astimezone(ET)
    if not sessions(local.date(), local.date()):
        return False
    opening, closing = session_bounds(local.date())
    return opening <= local < closing


def in_own_session(quote: dict | None, stamp: datetime) -> bool:
    """Whether a saved quote's own trading period (from its source) contains ``stamp``."""
    period = (quote or {}).get("session") or {}
    if not period.get("start") or not period.get("end"):
        return False
    return datetime.fromisoformat(period["start"]) <= stamp < datetime.fromisoformat(period["end"])


def fetch_symbols(selected, stamp: datetime):
    """Selected instruments worth fetching at ``stamp``: index futures wait while US stocks trade."""
    regular = us_regular_session(stamp)
    return [symbol for symbol in ordered(selected) if not (BY_SYMBOL[symbol].off_hours_only and regular)]


def shown_symbols(selected, quotes: dict, stamp: datetime):
    """Selected instruments the strip shows at ``stamp``; ``quotes`` maps symbol → saved quote data."""
    regular = us_regular_session(stamp)
    shown = []
    for symbol in ordered(selected):
        if BY_SYMBOL[symbol].off_hours_only and (regular or not in_own_session(quotes.get(symbol), stamp)):
            continue
        shown.append(symbol)
    return shown
