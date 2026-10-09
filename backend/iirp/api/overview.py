"""Typed, local-only Insider overview reads and durable stock quote requests."""

from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, BeforeValidator, Field

from iirp.api.insider_schemas import InsiderOwner, Output
from iirp.db import session
from iirp.insider.overview import overview
from iirp.messages import UserError

router = APIRouter(prefix="/api/v1/insider")
Index = Literal["all", "sp500", "nasdaq100"]


def _query_integer(value):
    # Query strings arrive as text; Literal[int] alone does not coerce them.
    return int(value) if isinstance(value, str) else value


Period = Annotated[Literal[7, 30, 90], BeforeValidator(_query_integer)]
ClusterWindow = Annotated[Literal[7, 14, 30], BeforeValidator(_query_integer)]
ClusterPeople = Annotated[Literal[2, 3], BeforeValidator(_query_integer)]


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
    # "not_applicable": no positive price to check (option exercises, awards, $0 rows).
    price_check: Literal["ok", "mismatch", "unchecked", "not_applicable"] = "unchecked"
    # Whole times the price differs from the market reference, for "mismatch" only.
    mismatch_ratio: str | None = None


class CompanyOverview(Output):
    issuer_id: str
    name: str
    ticker: str | None = None
    people: int
    buy_people: int
    sell_people: int
    buy_amount: str | None = None
    sell_amount: str | None = None
    net_amount: str | None = None
    amount_estimated: bool = False
    missing_price_rows: int = 0
    price_mismatch_rows: int = 0
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


class CompanySection(Output):
    """The first rows of one list, its full count and the trades left out of its amounts."""
    total: int
    items: list[CompanyOverview]
    price_mismatch_rows: int = 0


class TradeSection(Output):
    total: int
    items: list[OverviewTrade]
    price_mismatch_rows: int = 0


CompanySort = Literal["net_buy", "net_sell", "buy", "sell", "people"]


class InsiderOverview(Indices):
    as_of: str
    cluster_buys: CompanySection
    cluster_sales: CompanySection
    large_buys: TradeSection
    large_sales: TradeSection
    executive_buys: TradeSection
    holding_increases: TradeSection
    companies: CompanySection
    company_sort: CompanySort
    # Rows per "show more" step of the company list.
    company_page_rows: int
    price_keys: list[OverviewPriceKey]
    # Symbols whose quotes this response needs, for one merged quote request,
    # and how many of them have never been quoted yet.
    quote_symbols: list[str]
    quotes_pending: int
    # Ratio from which a reported price is marked as a mismatch, e.g. "10".
    price_mismatch_ratio: str


class FeedPrices(Output):
    items: dict[str, PriceComparison]
    price_mismatch_ratio: str


class StockQuotesInput(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=200)


class StockQuotesOutput(Output):
    requested: list[str]


@router.get("/overview", response_model=InsiderOverview)
def read_overview(index: Index = "all", days: Period = 30,
                  role: Literal["all", "executive", "director", "ten_percent"] = "all",
                  min_amount: Decimal = Query(default=Decimal(0), ge=0, le=Decimal("1e15")),
                  exclude_plans: bool = False, cluster_days: ClusterWindow = 7,
                  cluster_people: ClusterPeople = 2, exclude_cluster_plans: bool = True,
                  company_sort: CompanySort = "net_buy",
                  company_limit: int | None = Query(default=None, ge=1, le=2000)):
    with session() as s:
        return overview(s, index=index, days=days, role=role, min_amount=min_amount,
                        exclude_plans=exclude_plans, cluster_days=cluster_days,
                        cluster_people=cluster_people, exclude_cluster_plans=exclude_cluster_plans,
                        company_sort=company_sort, company_limit=company_limit)


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
