"""Read contracts for archived government transaction disclosures."""
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

Source = Literal["house", "senate", "oge"]


class PoliticalPersonView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    source: Source
    name: str
    district: str | None
    position: str | None


class PoliticalPersonSummary(PoliticalPersonView):
    report_count: int
    latest_report_date: date | None


class PoliticalPeopleOutput(BaseModel):
    items: list[PoliticalPersonSummary]


class PoliticalTransactionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    report_id: str
    page: int
    row_number: int
    owner: str
    asset_name: str
    ticker: str | None
    ticker_origin: str | None
    issuer_ticker: str | None
    issuer_ticker_origin: str | None
    asset_type: str
    option_type: str | None
    strike_price: Decimal | None
    expiration_date: date | None
    transaction_type: str
    transaction_date: date | None
    notification_date: date | None
    amount_low: Decimal | None
    amount_high: Decimal | None
    raw_row: str
    confidence: str
    warnings: list[str]


class PoliticalTransactionEntry(PoliticalTransactionView):
    source: Source
    # Submission/receipt date is independent of the official report date.
    filed_date: date | None
    report_date: date | None
    report_date_source: str | None
    person: PoliticalPersonView


class PoliticalTransactionsOutput(BaseModel):
    items: list[PoliticalTransactionEntry]
    total: int
    next_cursor: str | None


class PoliticalReportOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    source: Source
    external_id: str
    person: PoliticalPersonView
    # Submission/receipt date is independent of the official report date.
    filed_date: date | None
    report_date: date | None
    report_date_source: str | None
    url: str
    file_url: str | None
    source_hash: str | None
    amendment_of_id: str | None
    amendment_status: str
    status: str
    parser_version: str | None
    page_count: int
    pages_done: int
    ocr_pages: int
    ocr_seconds: float
    rows: list[PoliticalTransactionView]
    review_rows: list[PoliticalTransactionView]


class PoliticalSourceStatus(BaseModel):
    source: Source
    status: Literal["unknown", "available", "unavailable"]
    checking: bool
    last_error: str | None
    last_check_at: datetime | None
    next_check_at: datetime | None
    report_count: int
    active_report_count: int
    transaction_count: int
    review_count: int
    page_count: int
    pages_done: int
    ocr_pages: int
    ocr_seconds: float
    pending_reports: int


class PoliticalSourcesOutput(BaseModel):
    items: list[PoliticalSourceStatus]
