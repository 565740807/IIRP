"""Insider lookup, SEC fetch of one entity, and before/after-n price windows.

GETs read local data only; POSTs create durable jobs that the worker runs
under the SEC and Yahoo rate limits.
"""

import re
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from iirp.api.insider_schemas import Output
from iirp.api.schemas import CollectionOutput
from iirp.db import session
from iirp.messages import NotFoundError, UserError, msg

router = APIRouter(prefix="/api/v1/insider")

PRESETS = [3, 5, 10, 20]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LocalCompany(Output):
    cik: str
    name: str
    ticker: str | None = None
    last_trade: str | None = None


class LocalPerson(Output):
    cik: str
    name: str
    last_trade: str | None = None


class LocalLookupOutput(Output):
    query: str
    companies: list[LocalCompany]
    people: list[LocalPerson]


class LookupInput(Strict):
    query: str = Field(min_length=1, max_length=100)


class LookupCandidate(Output):
    cik: str
    name: str
    kind: Literal["company", "person"]
    tickers: list[str] = Field(default_factory=list)
    exchange: str | None = None
    local: bool = False


class LookupOutput(Output):
    job_id: str
    status: str
    error: str | None = None
    query: str
    candidates: list[LookupCandidate] = Field(default_factory=list)


class FetchInput(Strict):
    cik: str = Field(pattern=r"^[0-9]{1,10}$")
    kind: Literal["company", "person"]
    name: str | None = Field(default=None, max_length=200)
    months: int | None = Field(default=None, ge=1, le=120)
    start_date: date | None = None


class FetchState(Output):
    batch_id: str
    status: str
    created_at: str
    start_date: str | None = None
    discovered: int | None = None
    parsed: int | None = None
    error: str | None = None


class FetchStateOutput(Output):
    fetch: FetchState | None = None


class WindowChange(Output):
    value: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    status: str


class WindowItem(Output):
    ticker: str | None = None
    date: str | None = None
    symbol: str | None = None
    anchor_date: str | None = None
    before: WindowChange | None = None
    after: WindowChange | None = None


class TickerPrices(Output):
    status: Literal["ready", "fetching", "not_fetched", "unavailable"]
    security_id: str | None = None
    confirmed: bool = False
    reason: str | None = None
    fetched_at: str | None = None
    expires_at: str | None = None
    needed_start: str | None = None


class WindowsOutput(Output):
    n: int
    items: list[WindowItem]
    tickers: dict[str, TickerPrices]


class TradeKey(Strict):
    ticker: str = Field(min_length=1, max_length=32)
    date: date
    issuer_id: str | None = Field(default=None, max_length=10)


class PricesInput(Strict):
    n: int
    items: list[TradeKey] = Field(min_length=1, max_length=500)
    # True for a page the reader opened; the home feed's prices stay in the background.
    foreground: bool = False


class PricesOutput(Output):
    requested: list[str]
    batch_ids: list[str]


class WindowSettings(Output):
    default: int
    min: int
    max: int
    presets: list[int]


class RangeSettings(Output):
    months: int
    count: int


class InsiderSettingsOutput(Output):
    window_sessions: WindowSettings
    range: RangeSettings


def _n(value: int | None) -> int:
    from iirp.events.service import window_defaults

    limits = window_defaults()
    n = limits["insider"] if value is None else value
    if not limits["min"] <= n <= limits["max"]:
        raise HTTPException(422, msg("events.n_invalid", min=limits["min"], max=limits["max"]))
    return n


def _invoke(command, *args, **kwargs):
    try:
        return command(*args, **kwargs)
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (UserError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/settings", response_model=InsiderSettingsOutput)
def settings_view():
    """Defaults of the Insider pages: n (D16) and the lookup range (D15)."""
    from iirp.events.service import window_defaults
    from iirp.jobs.profiles import profiles

    limits = window_defaults()
    return {
        "window_sessions": {"default": limits["insider"], "min": limits["min"], "max": limits["max"],
                            "presets": PRESETS},
        "range": {"months": profiles()["normal_usage"]["insider_history_months"], "count": 10},
    }


@router.get("/lookup", response_model=LocalLookupOutput)
def lookup_local(q: str = Query(default="", max_length=100)):
    from iirp.insider.lookup import local_lookup

    with session() as s:
        return {"query": q.strip(), **_invoke(local_lookup, s, q)}


@router.post("/lookup", response_model=LookupOutput, status_code=202)
def lookup_sec(body: LookupInput):
    from iirp.insider.lookup import lookup_result, start_sec_lookup
    from iirp.jobs.providers import sec_configured

    if not sec_configured():
        raise HTTPException(409, msg("sec.user_agent_required"))
    job = _invoke(start_sec_lookup, body.query)
    with session() as s:
        return _invoke(lookup_result, s, job.id)


@router.get("/lookup/{job_id}", response_model=LookupOutput)
def lookup_status(job_id: str):
    from iirp.insider.lookup import lookup_result

    with session() as s:
        return _invoke(lookup_result, s, job_id)


@router.post("/fetch", response_model=CollectionOutput, status_code=202)
def fetch(body: FetchInput):
    from iirp.insider.lookup import fetch_entity

    return _invoke(fetch_entity, body.cik, body.kind, body.name, months=body.months, start_date=body.start_date)


@router.get("/fetch/{kind}/{cik}", response_model=FetchStateOutput)
def fetch_state(kind: Literal["company", "person"], cik: str):
    from iirp.insider.lookup import entity_fetch_state

    if not re.fullmatch(r"[0-9]{1,10}", cik):
        raise HTTPException(404, msg("http.not_found"))
    with session() as s:
        return {"fetch": entity_fetch_state(s, kind, cik)}


def _keys(items: str) -> list[dict]:
    """``TICKER|YYYY-MM-DD|issuer`` joined by commas."""
    result = []
    for part in [value for value in items.split(",") if value][:500]:
        fields = part.split("|")
        if len(fields) < 2 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fields[1]):
            raise HTTPException(422, msg("insider.window_items_invalid"))
        result.append({"ticker": fields[0], "date": fields[1], "issuer_id": fields[2] if len(fields) > 2 else None})
    return result


@router.get("/windows", response_model=WindowsOutput)
def windows(items: str = Query(default="", max_length=20000), n: int | None = None):
    """Before/after-n changes from the cached prices; never requests a provider."""
    from iirp.analysis.insider_windows import trade_windows

    keys = _keys(items)
    with session() as s:
        return trade_windows(s, keys, _n(n))


@router.post("/prices", response_model=PricesOutput, status_code=202)
def prices(body: PricesInput):
    """Fetch prices (one request per ticker, 24-hour cache) for windows still missing them."""
    from iirp.analysis.insider_windows import request_trade_prices

    n = _n(body.n)
    keys = [{"ticker": item.ticker, "date": item.date.isoformat(), "issuer_id": item.issuer_id}
            for item in body.items]
    with session() as s, s.begin():
        return _invoke(request_trade_prices, s, keys, n, foreground=body.foreground)


class Bar(Output):
    date: str
    open: str | None = None
    high: str | None = None
    low: str | None = None
    close: str | None = None
    volume: str | None = None


class BarsOutput(Output):
    ticker: str | None = None
    status: str
    bars: list[Bar] = Field(default_factory=list)
    fetched_at: str | None = None
    expires_at: str | None = None


@router.get("/bars", response_model=BarsOutput)
def bars(ticker: str = Query(max_length=32), start_date: date = Query(), end_date: date | None = None):
    """Cached daily bars (split-adjusted only) of a filing ticker, for the chart."""
    from sqlalchemy import select

    from iirp.analysis.insider_windows import provider_symbol
    from iirp.market.cache import current_cache, price_bars
    from iirp.models import Security

    symbol = provider_symbol(ticker)
    if not symbol:
        return {"ticker": None, "status": "unavailable"}
    with session() as s:
        security = s.scalar(select(Security).where(Security.symbol == symbol).order_by(Security.id).limit(1))
        cache = current_cache(s, security.id) if security else None
        if cache is None:
            return {"ticker": symbol, "status": "not_fetched"}
        rows, _ = price_bars(s, security.id, cache.id, ranges=[(start_date, end_date or date.max)])
        return {
            "ticker": symbol, "status": "ready",
            "bars": [row for row in rows if row["status"] == "VALID"],
            "fetched_at": cache.fetched_at.isoformat(), "expires_at": cache.expires_at.isoformat(),
        }
