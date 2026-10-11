"""Local long-term price inventory and durable management commands."""
from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from iirp.db import session
from iirp.market import long_prices

router = APIRouter(prefix="/api/v1/long-prices")


class RemovedMember(BaseModel):
    symbol: str
    removed_on: date


class HistoryGaps(BaseModel):
    symbol: str
    dates: list[date]
    accepted: bool


class LongPricesOutput(BaseModel):
    gap_symbols: int
    gap_days: int
    gaps: list[HistoryGaps]
    symbols: int
    active_symbols: int
    ready_symbols: int
    bars: int
    start_date: date | None
    end_date: date | None
    bytes: int
    updated_at: datetime | None
    status: Literal["paused", "backfilling", "ready"]
    history_years: int
    pending_jobs: int
    removed: list[RemovedMember]


class ManagementInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["all", "year", "half_year", "quarter", "refill"]
    confirmed: Literal[True]


class ManagementReceipt(BaseModel):
    job_id: str


@router.get("", response_model=LongPricesOutput)
def inventory():
    with session() as s:
        return long_prices.status(s)


@router.post("/manage", response_model=ManagementReceipt, status_code=202)
def manage(body: ManagementInput):
    with session() as s, s.begin():
        return {"job_id": long_prices.enqueue_manage(s, body.action)}
