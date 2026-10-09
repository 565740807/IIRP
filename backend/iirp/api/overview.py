"""Typed, local-only Insider overview reads and durable visible-stock quote requests."""

from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from iirp.api.insider_schemas import InsiderOwner, Output
from iirp.db import session
from iirp.messages import UserError

router = APIRouter(prefix="/api/v1/insider")
Index = Literal["all", "sp500", "nasdaq100"]


class PriceComparison(Output):
    price: str | None = None
    estimated: bool = False
    range_low: str | None = None
    range_high: str | None = None
    current_price: str | None = None
    change_percent: str | None = None
    relation: Literal["higher", "near", "lower"] | None = None
    quote_date: str | None = None
    quote_status: str | None = None
    amount: str | None = None
    amount_estimated: bool = False


class CompanyOverview(Output):
    issuer_id: str
    name: str
    ticker: str | None = None
    buy_people: int
    sell_people: int
    buy_amount: str | None = None
    sell_amount: str | None = None
    net_amount: str | None = None
    amount_estimated: bool = False
    missing_price_rows: int = 0
    buy_price: PriceComparison
    sell_price: PriceComparison
    start_date: str
    end_date: str


class OverviewTrade(Output):
    id: str
    issuer_id: str
    name: str
    ticker: str | None = None
    owners: list[InsiderOwner]
    date: str
    shares: str | None = None
    amount: str | None = None
    amount_estimated: bool = False
    holding_change: str | None = None
    price: PriceComparison


class IndexStatus(Output):
    index_name: str
    updated_at: str | None = None
    error: str | None = None


class Indices(Output):
    indices: list[IndexStatus]


class OverviewPriceKey(Output):
    ticker: str
    date: str
    issuer_id: str


class InsiderOverview(Indices):
    as_of: str
    cluster_buys: list[CompanyOverview]
    cluster_sales: list[CompanyOverview]
    large_buys: list[OverviewTrade]
    large_sales: list[OverviewTrade]
    executive_buys: list[OverviewTrade]
    holding_increases: list[OverviewTrade]
    companies: list[CompanyOverview]
    price_keys: list[OverviewPriceKey]


class FeedPrices(Output):
    items: dict[str, PriceComparison]


class StockQuotesInput(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=200)


class StockQuotesOutput(Output):
    requested: list[str]


@router.get("/overview", response_model=InsiderOverview)
def read_overview(index: Index = "all", days: Literal[7, 30, 90] = 30,
                  role: Literal["all", "executive", "director", "ten_percent"] = "all",
                  min_amount: Decimal = Query(default=Decimal(0), ge=0, le=Decimal("1e15")),
                  exclude_plans: bool = False, cluster_days: Literal[7, 14, 30] = 7,
                  cluster_people: Literal[2, 3] = 2, exclude_cluster_plans: bool = True):
    from iirp.insider.overview import overview
    with session() as s:
        return overview(s, index=index, days=days, role=role, min_amount=min_amount,
                        exclude_plans=exclude_plans, cluster_days=cluster_days,
                        cluster_people=cluster_people, exclude_cluster_plans=exclude_cluster_plans)


@router.get("/indices", response_model=Indices)
def read_indices():
    from iirp.insider.overview import index_status
    with session() as s:
        return {"indices": index_status(s)}


@router.get("/feed-prices", response_model=FeedPrices)
def read_feed_prices(revisions: str = Query(default="", max_length=8000)):
    from iirp.insider.prices import feed_prices
    ids = list(dict.fromkeys(filter(None, revisions.split(","))))
    if len(ids) > 200 or any(len(value) > 36 for value in ids):
        raise HTTPException(422, str(UserError("insider.window_items_invalid")))
    with session() as s:
        return feed_prices(s, ids)


@router.post("/quotes", status_code=202, response_model=StockQuotesOutput)
def request_quotes(body: StockQuotesInput):
    from iirp.market.stock_quotes import request_stock_quotes
    try:
        with session() as s, s.begin():
            request_stock_quotes(s, body.symbols)
        return {"requested": sorted(set(body.symbols))}
    except UserError as exc:
        raise HTTPException(422, str(exc)) from exc
