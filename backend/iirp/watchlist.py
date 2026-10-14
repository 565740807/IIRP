"""The user's watched stock codes and when their news was last acknowledged.

Codes are kept in the form they are compared in (``ticker_key``: upper case, "."
read as "-"), so BRK.B and BRK-B are one entry; at most ``LIMIT`` are kept. The
list and the acknowledgement time live in the database, so every browser sees the
same list and they are part of every backup.

Adding, importing and reading the list only read and write local tables: no
source is asked, and no price, identity or classification refresh is queued. A
code without any local data is kept as given; its name is simply unknown.
"""

import re
from datetime import datetime

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert

from iirp.insider.activity import ticker_key
from iirp.insider.overview import states_any, ticker_tokens
from iirp.messages import UserError
from iirp.models import (
    IndexConstituent,
    Issuer,
    LongPriceSeries,
    Preferences,
    Security,
    WatchlistItem,
    now,
)

LIMIT = 500
# A stock code: letters and digits, then up to two class suffixes after "-" (BRK-B, BF-B).
FORM = re.compile(r"[A-Z0-9]{1,10}(?:-[A-Z0-9]{1,5}){0,2}")
# Commas, spaces and new lines separate pasted codes (also full-width commas and semicolons).
SEPARATORS = re.compile(r"[\s,;，；、]+")
IMPORT_TEXT_LIMIT = 10_000
PREFERENCES_KEY = "watchlist"


def parse_ticker(value):
    """The compared form of one code, or None when it is not a stock code; a leading "$" is dropped."""
    key = ticker_key(value).removeprefix("$")
    return key if FORM.fullmatch(key) else None


def _lock(s):
    # Adds are serialized so the limit holds when two browsers add at once.
    s.execute(text("SELECT pg_advisory_xact_lock(hashtext('iirp.watchlist'))"))


def keys(s):
    return list(s.scalars(select(WatchlistItem.ticker).order_by(WatchlistItem.ticker)))


def _names(s, wanted):
    """Code → (name, issuer id) from local tables: the index lists (ordinary case),
    then saved securities, then SEC issuers; a code none of them knows is absent."""
    if not wanted:
        return {}
    names, issuers = {}, {}
    normal = func.upper(func.replace(IndexConstituent.ticker, ".", "-"))
    for ticker, name, cik in s.execute(select(IndexConstituent.ticker, IndexConstituent.name, IndexConstituent.cik)
                                       .where(normal.in_(wanted)).order_by(IndexConstituent.index_name)):
        names.setdefault(ticker_key(ticker), name)
        if cik:
            issuers.setdefault(ticker_key(ticker), cik)
    symbol = func.upper(func.replace(Security.symbol, ".", "-"))
    for ticker, name in s.execute(select(Security.symbol, Security.name).where(symbol.in_(wanted), Security.name != "")):
        names.setdefault(ticker_key(ticker), name)
    for issuer, name, ticker in s.execute(
            select(Issuer.id, Issuer.name, Issuer.ticker).where(states_any(Issuer.ticker, wanted)).order_by(Issuer.id)):
        for key in ticker_tokens(ticker) & set(wanted):
            names.setdefault(key, name)
            issuers.setdefault(key, issuer)
    series = set(s.scalars(select(func.upper(LongPriceSeries.symbol)).where(
        func.upper(LongPriceSeries.symbol).in_(wanted))))
    return {key: (names.get(key), issuers.get(key), key in names or key in issuers or key in series)
            for key in wanted}


def _saved(s):
    row = s.get(Preferences, 1)
    return ((row.values or {}).get(PREFERENCES_KEY) or {}) if row else {}


def acknowledged_at(s):
    value = _saved(s).get("acknowledged_at")
    return datetime.fromisoformat(value) if value else None


def state(s):
    rows = list(s.execute(select(WatchlistItem.ticker, WatchlistItem.added_at).order_by(WatchlistItem.ticker)))
    known = _names(s, [row.ticker for row in rows])
    return {
        "items": [{"ticker": row.ticker, "name": known[row.ticker][0], "issuer_id": known[row.ticker][1],
                   "local": known[row.ticker][2], "added_at": row.added_at} for row in rows],
        "limit": LIMIT,
        "acknowledged_at": acknowledged_at(s),
    }


def _insert(s, wanted, at):
    """Insert the codes not yet watched, in the given order, while room remains;
    returns (added, already watched, left out for the limit)."""
    _lock(s)
    present = set(s.scalars(select(WatchlistItem.ticker).where(WatchlistItem.ticker.in_(wanted)))) if wanted else set()
    room = LIMIT - (s.scalar(select(func.count()).select_from(WatchlistItem)) or 0)
    added, existing, over = [], [], []
    for key in wanted:
        if key in present:
            existing.append(key)
        elif len(added) < room:
            added.append(key)
        else:
            over.append(key)
    if added:
        s.execute(insert(WatchlistItem).values([{"ticker": key, "added_at": at} for key in added])
                  .on_conflict_do_nothing(index_elements=[WatchlistItem.ticker]))
    return added, existing, over


def add(s, ticker):
    key = parse_ticker(ticker)
    if key is None:
        raise UserError("watchlist.ticker_invalid", ticker=ticker.strip()[:24])
    added, existing, over = _insert(s, [key], now())
    if over:
        raise UserError("watchlist.full", limit=LIMIT)
    return key


def remove(s, ticker):
    s.execute(delete(WatchlistItem).where(WatchlistItem.ticker == ticker_key(ticker).removeprefix("$")))


def import_text(s, value):
    """Add every code of a pasted text; report which were added, already watched,
    not codes at all, or left out because the list is full."""
    if len(value) > IMPORT_TEXT_LIMIT:
        raise UserError("watchlist.import_too_long", limit=IMPORT_TEXT_LIMIT)
    wanted, invalid = [], []
    for token in filter(None, SEPARATORS.split(value)):
        key = parse_ticker(token)
        if key is None:
            if token[:24] not in invalid:
                invalid.append(token[:24])
        elif key not in wanted:
            wanted.append(key)
    added, existing, over = _insert(s, wanted, now())
    return {"added": added, "existing": existing, "invalid": invalid, "over_limit": over}


def acknowledge(s, at):
    """Remember that news up to ``at`` (never later than now) has been seen; an older time never replaces a newer one."""
    from iirp.jobs.batches import defaults

    at = min(at, now())
    defaults(s)
    row = s.get(Preferences, 1, with_for_update=True)
    saved = (row.values or {}).get(PREFERENCES_KEY) or {}
    previous = saved.get("acknowledged_at")
    if previous and datetime.fromisoformat(previous) >= at:
        return datetime.fromisoformat(previous)
    row.values = {**row.values, PREFERENCES_KEY: {**saved, "acknowledged_at": at.isoformat()}}
    row.version += 1
    return at


def since(s):
    """Each watched code's start of news: the later of when it was added and the last acknowledgement."""
    seen = acknowledged_at(s)
    return {row.ticker: max(row.added_at, seen) if seen else row.added_at
            for row in s.execute(select(WatchlistItem.ticker, WatchlistItem.added_at))}
