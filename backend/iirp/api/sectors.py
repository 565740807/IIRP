"""Sector performance, K-line candles and the one daily-price request of the sector page.

GETs read the local daily cache and saved quotes only; switching levels, views,
periods, ranges or sorting never reaches a provider. The industry group ETFs
are only asked for by the group level's own price request. Quotes are asked for through the
shared stock quote request (POST /api/v1/insider/quotes) like every other page.
"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from iirp.api.insider_schemas import Output
from iirp.db import session
from iirp.messages import NotFoundError, UserError

router = APIRouter(prefix="/api/v1/sectors")

Period = Literal["today", "1w", "1m", "3m", "ytd"]


class SectorChange(Output):
    # Price change as a fraction ("0.012345" is +1.2345%), None when it cannot be computed.
    value: str | None = None
    # Sessions compared: the start session's close against the end session's close
    # (or, for an intraday "today", against the quote).
    start_date: str | None = None
    end_date: str | None = None
    status: Literal["available", "missing", "before_listing", "no_prices"]


class SectorToday(SectorChange):
    mode: Literal["intraday", "close"]
    as_of: str | None = None
    delayed: bool = False


class SectorPeriods(Output):
    today: SectorToday
    week: SectorChange
    month: SectorChange
    quarter: SectorChange
    ytd: SectorChange


class SectorWeights(Output):
    today: float | None = None
    week: float | None = None
    month: float | None = None
    quarter: float | None = None
    ytd: float | None = None


class PriceFetch(Output):
    # "ready": a current 24-hour cache; "fetching": today's request is running;
    # "no_data": the provider returned no daily prices; "failed": today's
    # request ended without a cache (reason is a message); "not_requested".
    status: Literal["ready", "fetching", "no_data", "failed", "not_requested"]
    reason: str | None = None


class EtfPerformance(Output):
    etf: str
    periods: SectorPeriods
    first_date: str | None = None
    # Complete past years in the history window that the ETF traded from January.
    full_years: int
    quote_status: Literal["live", "closed", "delayed", "missing"]
    quote_time: str | None = None
    price_fetched_at: str | None = None
    price_expires_at: str | None = None
    prices_current: bool
    fetch: PriceFetch


class SectorItem(EtfPerformance):
    id: str
    # Heatmap tile weight per period, between the configured minimum and 1.
    weights: SectorWeights
    # Industry groups listed for the sector (its drill-down).
    groups: int


class SectorRanks(Output):
    today: int | None = None
    week: int | None = None
    month: int | None = None
    quarter: int | None = None
    ytd: int | None = None


class SectorGroupItem(Output):
    id: str
    sector: str
    coverage: Literal["complete", "partial", "reference", "pending"]
    # The sector's only industry group, shown with the sector's own ETF.
    same_as_sector: bool
    # The primary ETF, the only one ranked; None (with no periods) when pending.
    etf: str | None = None
    periods: SectorPeriods | None = None
    first_date: str | None = None
    full_years: int = 0
    quote_status: Literal["live", "closed", "delayed", "missing"] = "missing"
    quote_time: str | None = None
    price_fetched_at: str | None = None
    price_expires_at: str | None = None
    prices_current: bool = False
    fetch: PriceFetch | None = None
    # Heatmap weight and rank (1 = largest change) among the primaries shown.
    weights: SectorWeights | None = None
    ranks: SectorRanks | None = None
    # Reference rows: shown with the same periods, never ranked.
    alternates: list[EtfPerformance]


class SectorPerformance(Output):
    as_of: str
    market_period: Literal["regular", "pre", "post", "closed"]
    last_completed_session: str
    history_start: str
    history_years: int
    # Sorted by today's change, largest first, unknown last.
    items: list[SectorItem]
    # Sector ids in the configured order (the items are sorted by today's change).
    sector_ids: list[str]
    quote_symbols: list[str]
    # Every industry group ETF (names only; their prices are read by the group level).
    group_etfs: list[str]
    prices_pending: list[str]


class SectorGroups(Output):
    as_of: str
    market_period: Literal["regular", "pre", "post", "closed"]
    last_completed_session: str
    history_start: str
    history_years: int
    # The sector whose groups these are; None for all industry groups.
    sector: str | None = None
    # Ranked groups by today's change, largest first, then unknown, then pending.
    items: list[SectorGroupItem]
    quote_symbols: list[str]
    prices_pending: list[str]


class SectorCandle(Output):
    date: str
    end_date: str
    open: str
    high: str
    low: str
    close: str
    sessions: int


class SectorCandles(Output):
    id: str
    sector: str
    etf: str
    candles: list[SectorCandle]
    first_date: str | None = None
    price_fetched_at: str | None = None


class SectorCandlesOutput(Output):
    range: Literal["3m", "6m", "1y", "ytd"]
    interval: Literal["day", "week", "month"]
    level: Literal["sector", "group"]
    sector: str | None = None
    start_date: str
    end_date: str
    items: list[SectorCandles]


class SectorPricesInput(BaseModel):
    # True for the sector page the reader opened; the home strip asks in the background.
    foreground: bool = False
    # "group" asks for the industry group ETFs (of one sector, or of all of them).
    level: Literal["sector", "group"] = "sector"
    sector: str | None = Field(default=None, max_length=64)


class SectorPricesOutput(Output):
    requested: list[str]
    batch_ids: list[str]


# Response field names for the period codes used in URLs and config.
FIELDS = {"today": "today", "1w": "week", "1m": "month", "3m": "quarter", "ytd": "ytd"}


def _renamed(values: dict) -> dict:
    return {FIELDS[key]: value for key, value in values.items()}


@router.get("", response_model=SectorPerformance)
def read_performance():
    from iirp.analysis.sectors import performance

    with session() as s:
        result = performance(s)
    for item in result["items"]:
        item["periods"], item["weights"] = _renamed(item["periods"]), _renamed(item["weights"])
    return result


def _known_sector(sector: str | None):
    from iirp.analysis.sectors import sector_ids

    if sector is not None and sector not in sector_ids():
        raise HTTPException(404, str(NotFoundError("sectors.unknown", sector=sector)))


@router.get("/groups", response_model=SectorGroups)
def read_groups(sector: str | None = Query(default=None, max_length=64)):
    """Industry groups of one sector, or all of them; reads the local cache only."""
    from iirp.analysis.sectors import group_performance

    _known_sector(sector)
    with session() as s:
        result = group_performance(s, sector=sector)
    for item in result["items"]:
        for row in (item, *item["alternates"]):
            if row.get("periods") is not None:
                row["periods"] = _renamed(row["periods"])
        if item["weights"] is not None:
            item["weights"], item["ranks"] = _renamed(item["weights"]), _renamed(item["ranks"])
    return result


@router.get("/candles", response_model=SectorCandlesOutput)
def read_candles(range: Literal["3m", "6m", "1y", "ytd"] = "3m",
                 interval: Literal["day", "week", "month"] = "day",
                 level: Literal["sector", "group"] = "sector",
                 sector: str | None = Query(default=None, max_length=64)):
    from iirp.analysis.sectors import candles

    _known_sector(sector)
    with session() as s:
        return candles(s, chart_range=range, interval=interval, level=level,
                       sector=sector if level == "group" else None)


@router.post("/prices", response_model=SectorPricesOutput, status_code=202)
def request_prices(body: SectorPricesInput):
    from iirp.analysis.sectors import request_prices as plan

    _known_sector(body.sector)
    try:
        with session() as s, s.begin():
            return plan(s, foreground=body.foreground, level=body.level, sector=body.sector)
    except UserError as exc:
        raise HTTPException(409, str(exc)) from exc
