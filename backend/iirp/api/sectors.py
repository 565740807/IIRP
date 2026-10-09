"""Sector performance, K-line candles and the one daily-price request of the sector page.

GETs read the local daily cache and saved quotes only; switching views, periods,
ranges or sorting never reaches a provider. Quotes are asked for through the
shared stock quote request (POST /api/v1/insider/quotes) like every other page.
"""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from iirp.api.insider_schemas import Output
from iirp.db import session
from iirp.messages import UserError

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


class SectorItem(Output):
    id: str
    etf: str
    periods: SectorPeriods
    # Heatmap tile weight per period, between the configured minimum and 1.
    weights: SectorWeights
    first_date: str | None = None
    # Complete past years in the history window that the ETF traded from January.
    full_years: int
    quote_status: Literal["live", "closed", "delayed", "missing"]
    quote_time: str | None = None
    price_fetched_at: str | None = None
    price_expires_at: str | None = None
    prices_current: bool


class SectorPerformance(Output):
    as_of: str
    market_period: Literal["regular", "pre", "post", "closed"]
    last_completed_session: str
    history_start: str
    history_years: int
    # Sorted by today's change, largest first, unknown last.
    items: list[SectorItem]
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
    etf: str
    candles: list[SectorCandle]
    first_date: str | None = None
    price_fetched_at: str | None = None


class SectorCandlesOutput(Output):
    range: Literal["3m", "6m", "1y", "ytd"]
    interval: Literal["day", "week", "month"]
    start_date: str
    end_date: str
    items: list[SectorCandles]


class SectorPricesInput(BaseModel):
    # True for the sector page the reader opened; the home strip asks in the background.
    foreground: bool = False


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


@router.get("/candles", response_model=SectorCandlesOutput)
def read_candles(range: Literal["3m", "6m", "1y", "ytd"] = "3m",
                 interval: Literal["day", "week", "month"] = "day"):
    from iirp.analysis.sectors import candles

    with session() as s:
        return candles(s, chart_range=range, interval=interval)


@router.post("/prices", response_model=SectorPricesOutput, status_code=202)
def request_prices(body: SectorPricesInput):
    from iirp.analysis.sectors import request_prices as plan

    try:
        with session() as s, s.begin():
            return plan(s, foreground=body.foreground)
    except UserError as exc:
        raise HTTPException(409, str(exc)) from exc
