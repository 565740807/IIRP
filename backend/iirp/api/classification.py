"""Index lists (weekly update state, update now) and a security's sector classification."""

from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel, ConfigDict

from iirp.db import session
from iirp.messages import UserError

router = APIRouter(prefix="/api/v1")
SYMBOL = Path(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9.^=-]{0,31}$")
IndexName = Literal["sp500", "nasdaq100"]


class Output(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class IndexList(Output):
    index_name: IndexName
    members: int
    updated_at: datetime | None = None
    attempted_at: datetime | None = None
    # Message of the last failed attempt after the last success; the old list stays in use.
    error: str | None = None
    running: bool
    # Now when due, the retry time after a failure, otherwise the next weekly time.
    next_run_at: datetime


class IndexListsOutput(Output):
    """The two unofficial index lists and their fixed weekly update time (US Eastern)."""
    items: list[IndexList]
    weekday: Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    time: str
    timezone: str
    retry_seconds: int
    next_scheduled_at: datetime


class SectorRef(Output):
    id: str
    etf: str


class GroupRef(Output):
    id: str
    # The group's primary ETF; None when no suitable ETF exists (coverage pending).
    etf: str | None = None
    coverage: Literal["complete", "partial", "reference", "pending"]


class ClassificationOutput(Output):
    """Sector of a security: the S&P 500 list's GICS first, then Yahoo's sector, else none.

    ``refresh`` is ``needed`` when the identity was checked before Yahoo's sector was
    kept (the page may ask once), ``pending`` while that refresh runs.
    """
    symbol: str
    source: Literal["sp500_gics", "yahoo", "none"]
    sector: SectorRef | None = None
    industry_group: GroupRef | None = None
    gics_sector: str | None = None
    gics_sub_industry: str | None = None
    yahoo_sector: str | None = None
    yahoo_industry: str | None = None
    indexes: list[IndexName]
    removed_on: date | None = None
    refresh: Literal["none", "needed", "pending"]


@router.get("/index-constituents", response_model=IndexListsOutput)
def index_lists():
    from iirp.market.constituents import status

    with session() as s:
        return status(s)


@router.post("/index-constituents/refresh", response_model=IndexListsOutput, status_code=202)
def index_lists_refresh():
    """Update both lists now (one job each unless one already runs)."""
    from iirp.market.constituents import SOURCES, enqueue, status

    with session() as s, s.begin():
        enqueue(s, list(SOURCES), trigger="manual")
    with session() as s:
        return status(s)


@router.get("/securities/{symbol}/classification", response_model=ClassificationOutput)
def classification(symbol: str = SYMBOL):
    from iirp.market.classification import classify

    with session() as s:
        return classify(s, symbol)


@router.post("/securities/{symbol}/classification/refresh", response_model=ClassificationOutput, status_code=202)
def classification_refresh(symbol: str = SYMBOL):
    """Ask once for the identity refresh a classification is waiting for; otherwise nothing."""
    from iirp.market.classification import request_refresh

    try:
        with session() as s, s.begin():
            return request_refresh(s, symbol)
    except UserError as exc:
        raise HTTPException(409, str(exc)) from exc
