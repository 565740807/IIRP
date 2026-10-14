"""Followed people and their trades: list, add, remove, restore the defaults, the merged
timeline and one person's page. Local reads and writes only; nothing is fetched."""

from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from iirp.api.insider_schemas import Output
from iirp.db import session
from iirp.messages import NotFoundError, UserError, msg

router = APIRouter(prefix="/api/v1/notable")

Source = Literal["sec", "house", "oge"]
Category = Literal["investor", "investment_firm", "founder_ceo", "house", "president", "cabinet"]


class Note(Output):
    en: str
    zh: str


class NotablePersonEntry(Output):
    # sec:<10-digit CIK>, house:<name>|<district> or oge:<name>|<position>.
    key: str
    source: Source
    name: str
    # Null for an SEC filer added by the user (no category in the default list).
    category: Category | None = None
    # One sentence on who the person is, from the default list.
    note: Note | None = None
    cik: str | None = None
    district: str | None = None
    position: str | None = None
    # Listed in config/notable-people.toml.
    default: bool
    # Local filings or reports name this person.
    local: bool
    watched: bool
    added_at: datetime | None = None


class NotablePeopleOutput(Output):
    items: list[NotablePersonEntry]
    limit: int
    default_count: int


class NotableAdd(BaseModel):
    key: str = Field(min_length=5, max_length=300)
    # Shown for an SEC filer that no local filing names yet.
    name: str | None = Field(default=None, max_length=200)


class NotableRestoreOutput(NotablePeopleOutput):
    added: int


class TradePerson(Output):
    key: str
    name: str
    watched: bool
    category: Category | None = None
    # ceo, cfo, officer, director, ten_percent, other (SEC); house; president, vice_president,
    # attorney_general, secretary.<department>, administrator.<agency>, official (OGE).
    roles: list[str]
    # The officer title as filed, or the OGE position.
    title: str | None = None
    district: str | None = None


class NotableTrade(Output):
    id: str
    source: Source
    # SEC: the US Eastern day the SEC accepted the filing; House and OGE: the report date.
    published: date | None
    published_at: datetime | None = None
    person: TradePerson
    # Other followed filers named on the same SEC row (a person and their fund).
    others: list[TradePerson]
    ticker: str | None
    # The company (SEC) or the asset as reported (House, OGE).
    asset_name: str | None
    # The SEC issuer: the filing's company, or for a government row the issuer stating its code.
    issuer_id: str | None
    asset_kind: Literal["stock", "bond", "option", "fund", "other"]
    side: Literal["buy", "sell", "exchange", "unknown"]
    partial: bool
    # 10b5-1 plan (SEC): true, false when the filing says no, null when it does not say.
    plan: bool | None
    shares: str | None
    price: str | None
    # SEC: filed shares times filed price.
    amount: str | None
    # House and OGE: the reported value range; no high end for "over" ranges.
    amount_low: str | None
    amount_high: str | None
    currency: str | None
    transaction_date: date | None
    # Days from the trade to its publication.
    lag_days: int | None
    # The original filing or report on the source's public site.
    url: str | None
    accession: str | None
    report_id: str | None
    amendment: bool
    # House and OGE: self, spouse, joint or dependent, as reported.
    owner: str | None
    # Read from a scanned page.
    ocr: bool
    # Stated doubts: direction_unknown, direction_corrected, ticker_inferred, date_missing,
    # date_after_report, date_unlikely, amount_missing, amount_unusual, option_incomplete, asset_unreadable,
    # text_garbled.
    review: list[str]
    # The code the before/after windows use (an option's underlying stock); null for bonds,
    # funds, rows of an unknown kind and rows without a code, with the reason in price_none.
    price_ticker: str | None
    price_none: Literal["bond", "fund", "other", "no_ticker"] | None
    # The close the after-publication window starts from: the last close before the market could
    # react (after the close: that close; before or during a session, or a report date: the close before).
    published_basis: date | None


class NotableTradesOutput(Output):
    items: list[NotableTrade]
    total: int
    next_cursor: str | None


class NotableReport(Output):
    id: str
    source: Source
    published: date | None
    published_at: datetime | None
    person: TradePerson
    others: list[TradePerson]
    url: str | None
    amendment: bool
    # Whole-report counts, including rows excluded by the active row filters.
    counts: dict[str, int]
    equities: int
    sides: dict[str, int]
    preview_ids: list[str]
    trades: list[NotableTrade]


class NotableReportsOutput(Output):
    items: list[NotableReport]
    total: int
    next_cursor: str | None


class TradeReport(Output):
    id: str
    report_date: date | None
    filed_date: date | None
    url: str
    file_url: str | None
    superseded: bool
    amendment: bool
    ocr_pages: int
    page_count: int


class PoliticalTradeOutput(Output):
    trade: NotableTrade
    # The row as the report prints it (or as read from the scan).
    raw_row: str
    page: int
    row_number: int
    asset_type: str
    # reported, local_exact (matched by name) or inferred.
    ticker_origin: str | None
    option_type: str | None
    strike_price: str | None
    expiration_date: date | None
    notification_date: date | None
    report: TradeReport


class SummaryStat(Output):
    # N: trades whose window has a value.
    count: int
    # How many of them rose.
    up: int
    # Null below ``small`` values; ``values`` lists them instead.
    median: str | None
    values: list[str]
    # Trades whose n-th session has not closed yet, and trades without stored prices.
    pending: int
    missing: int


class SummarySide(Output):
    # Coded stock trades on this side.
    trades: int
    # From the trade date, and from the publication date.
    after: SummaryStat
    published: SummaryStat


class SummarySides(Output):
    buy: SummarySide
    sell: SummarySide


class PersonSummaryOutput(Output):
    n: int
    small: int
    sides: SummarySides
    # Buys and sales read, and those without a stock code (bonds, funds, no code).
    rows: int
    no_code: int


class PoliticalReportLink(Output):
    id: str
    report_date: date | None
    url: str
    file_url: str | None
    amendment: bool
    superseded: bool
    status: str
    ocr_pages: int
    page_count: int


class SecFilings(Output):
    # Local Form 4 filings naming this filer.
    count: int
    latest_at: datetime | None
    edgar_url: str


class NotablePersonOutput(NotablePersonEntry):
    roles: list[str]
    title: str | None = None
    reports: list[PoliticalReportLink]
    filings: SecFilings | None = None


def _people(s):
    from iirp.notable.people import LIMIT, defaults, watched

    return {"items": watched(s), "limit": LIMIT, "default_count": len(defaults())}


@router.get("/people", response_model=NotablePeopleOutput)
def read_people(scope: Literal["watched", "all"] = "watched"):
    """The followed people; ``all`` adds everyone the "all" timeline covers, unmarked."""
    from iirp.notable.people import LIMIT, defaults, directory

    with session() as s:
        if scope == "all":
            return {"items": directory(s), "limit": LIMIT, "default_count": len(defaults())}
        return _people(s)


@router.post("/people", response_model=NotablePeopleOutput)
def add_person(body: NotableAdd):
    from iirp.notable.people import add

    try:
        with session() as s, s.begin():
            add(s, body.key, body.name)
            return _people(s)
    except UserError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/people", response_model=NotablePeopleOutput)
def remove_person(key: str = Query(min_length=5, max_length=300)):
    from iirp.notable.people import parse_key, remove

    try:
        with session() as s, s.begin():
            parse_key(key)
            remove(s, key)
            return _people(s)
    except UserError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/people/restore", response_model=NotableRestoreOutput)
def restore_defaults():
    """Add back the default people who were removed; people the user added stay."""
    from iirp.notable.people import restore

    with session() as s, s.begin():
        added = restore(s)
        return {**_people(s), "added": added}


@router.get("/person", response_model=NotablePersonOutput)
def read_person(key: str = Query(min_length=5, max_length=300)):
    from iirp.notable.trades import person_view

    try:
        with session() as s:
            return person_view(s, key)
    except UserError as exc:
        raise HTTPException(422, str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/trades", response_model=NotableTradesOutput)
def read_trades(scope: Literal["watched", "all"] = "watched",
                person: str | None = Query(default=None, min_length=5, max_length=300),
                source: Source | None = None,
                direction: Literal["buy", "sell", "exchange"] | None = None,
                ticker: str | None = Query(default=None, max_length=24),
                issuer_id: str | None = Query(default=None, max_length=10),
                published_from: date | None = None, published_to: date | None = None,
                limit: int = Query(default=25, ge=1, le=100),
                cursor: str = Query(default="", max_length=400)):
    """Followed people's trades from the three sources, newest publication first."""
    from iirp.notable.trades import trades

    try:
        with session() as s:
            return trades(s, scope=scope, person=person, source=source, direction=direction, ticker=ticker,
                          issuer_id=issuer_id, published_from=published_from, published_to=published_to,
                          limit=limit, cursor=cursor)
    except UserError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/trades/{transaction_id}", response_model=PoliticalTradeOutput)
def read_political_trade(transaction_id: str):
    """One House or OGE row with its report, for the trade detail page."""
    from iirp.notable.trades import political_trade

    if len(transaction_id) > 36:
        raise HTTPException(404, msg("notable.trade_not_found"))
    try:
        with session() as s:
            return political_trade(s, transaction_id)
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/reports", response_model=NotableReportsOutput)
def read_reports(scope: Literal["watched", "all"] = "watched",
                 assets: Literal["equities", "all"] = "equities",
                 person: str | None = Query(default=None, min_length=5, max_length=300),
                 source: Source | None = None,
                 direction: Literal["buy", "sell", "exchange"] | None = None,
                 ticker: str | None = Query(default=None, max_length=24),
                 published_from: date | None = None, published_to: date | None = None,
                 limit: int = Query(default=5, ge=1, le=25),
                 cursor: str = Query(default="", max_length=400)):
    """One filing or government report per group; row filters never split a group across pages."""
    from iirp.notable.reports import reports

    try:
        with session() as s:
            return reports(s, scope=scope, person=person, source=source, direction=direction, ticker=ticker,
                           published_from=published_from, published_to=published_to, assets=assets, limit=limit, cursor=cursor)
    except UserError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/summary", response_model=PersonSummaryOutput)
def read_summary(key: str = Query(min_length=5, max_length=300), n: int | None = None):
    """How one person's coded stock trades moved n sessions after the trade and after publication."""
    from iirp.api.insider import _n
    from iirp.notable.trades import person_summary

    window = _n(n)
    try:
        with session() as s:
            return person_summary(s, key, window)
    except UserError as exc:
        raise HTTPException(422, str(exc)) from exc
