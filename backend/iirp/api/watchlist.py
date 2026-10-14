"""The watched stock codes: list, add, remove, import and acknowledge; local reads and writes only."""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel, Field

from iirp.api.insider_schemas import Output
from iirp.db import session
from iirp.messages import UserError

router = APIRouter(prefix="/api/v1/watchlist")


class WatchlistEntry(Output):
    # Upper case with "." read as "-" (BRK-B).
    ticker: str
    # The name a local table gives the code (index lists, saved securities, SEC issuers).
    name: str | None = None
    issuer_id: str | None = None
    # Whether any local table knows the code; a code without local data is still kept.
    local: bool
    added_at: datetime


class WatchlistOutput(Output):
    items: list[WatchlistEntry]
    limit: int
    # News up to this time has been acknowledged ("seen"); null before the first time.
    acknowledged_at: datetime | None = None


class WatchlistAdd(BaseModel):
    ticker: str = Field(min_length=1, max_length=32)


class WatchlistImport(BaseModel):
    text: str = Field(max_length=10_000)


class WatchlistImportOutput(WatchlistOutput):
    added: list[str]
    existing: list[str]
    # Pasted words that are not stock codes, as given.
    invalid: list[str]
    # Codes left out because the list reached its limit.
    over_limit: list[str]


class NewsOwner(Output):
    id: str
    name: str


class NewsTrader(Output):
    """The new trades of the same filing owners on one side."""
    # Their newest transaction (its detail page is /transactions/{id}).
    id: str
    side: Literal["buy", "sell"]
    owners: list[NewsOwner]
    # The counted amount; null when no trade has a usable price.
    amount: str | None = None
    # Some trades have no usable price and are left out of the amount.
    amount_partial: bool = False
    amount_estimated: bool = False
    # Some trades have a suspect price: shown, their amounts not counted.
    price_mismatch: bool = False
    trades: int
    first_date: str
    last_date: str
    # When the SEC accepted the newest (for a correction, the original filing's time).
    accepted_at: datetime


class InsiderNews(Output):
    ticker: str
    issuer_id: str
    name: str
    # News of this code starts here: the later of when it was added and the last acknowledgement.
    since: datetime
    latest_at: datetime
    # Transactions, counted one by one.
    trades: int
    buys: int
    sales: int
    # One line per insider and side, largest amount first (at most five), and how many there are.
    traders: list[NewsTrader]
    trader_count: int


class WatchlistNews(Output):
    # Read up to this time; acknowledging sends it back.
    as_of: datetime
    acknowledged_at: datetime | None = None
    # Codes with new open-market purchases or sales, the newest first.
    insider: list[InsiderNews]


class WatchlistAcknowledge(BaseModel):
    at: datetime


def _state(s):
    from iirp.watchlist import state

    return state(s)


@router.get("", response_model=WatchlistOutput)
def read_watchlist():
    with session() as s:
        return _state(s)


@router.post("", response_model=WatchlistOutput)
def add_to_watchlist(body: WatchlistAdd):
    from iirp.watchlist import add

    try:
        with session() as s, s.begin():
            add(s, body.ticker)
            return _state(s)
    except UserError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/{ticker}", response_model=WatchlistOutput)
def remove_from_watchlist(ticker: str = Path(min_length=1, max_length=32)):
    from iirp.watchlist import remove

    with session() as s, s.begin():
        remove(s, ticker)
        return _state(s)


@router.post("/import", response_model=WatchlistImportOutput)
def import_watchlist(body: WatchlistImport):
    from iirp.watchlist import import_text

    try:
        with session() as s, s.begin():
            result = import_text(s, body.text)
            return {**_state(s), **result}
    except UserError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/news", response_model=WatchlistNews)
def read_news():
    """New open-market Insider purchases and sales of the watched codes since each code's
    start (the later of when it was added and the last acknowledgement); local reads only."""
    from iirp.insider.activity import new_trades
    from iirp.models import now
    from iirp.watchlist import acknowledged_at, since

    at = now()
    with session() as s:
        return {"as_of": at, "acknowledged_at": acknowledged_at(s), "insider": new_trades(s, since(s), at=at)}


@router.put("/acknowledged", response_model=WatchlistOutput)
def acknowledge_watchlist(body: WatchlistAcknowledge):
    """News up to ``at`` (the time the page read it) has been seen."""
    from iirp.watchlist import acknowledge

    if body.at.tzinfo is None:
        raise HTTPException(422, str(UserError("watchlist.time_without_zone")))
    with session() as s, s.begin():
        acknowledge(s, body.at)
        return _state(s)
