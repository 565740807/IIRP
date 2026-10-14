"""Government transaction disclosures, retained independently of SEC filings."""
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from iirp.models.base import Base, now, uid


class PoliticalPerson(Base):
    __tablename__ = "political_person"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    source: Mapped[str] = mapped_column(String(16))
    identity: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    district: Mapped[str | None] = mapped_column(String(16))
    position: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("source", "identity", name="uq_political_person_identity"),)


class PoliticalReport(Base):
    __tablename__ = "political_report"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    source: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str] = mapped_column(Text)
    person_id: Mapped[str] = mapped_column(ForeignKey("political_person.id"), index=True)
    filed_date: Mapped[date | None] = mapped_column(Date, index=True)
    report_date: Mapped[date | None] = mapped_column(Date, index=True)
    report_date_source: Mapped[str | None] = mapped_column(String(24))
    url: Mapped[str] = mapped_column(Text)
    source_hash: Mapped[str | None] = mapped_column(ForeignKey("source_object.sha256"))
    amendment_of_id: Mapped[str | None] = mapped_column(ForeignKey("political_report.id"))
    amendment_status: Mapped[str] = mapped_column(String(24), default="original")
    status: Mapped[str] = mapped_column(String(24), default="discovered")
    parser_version: Mapped[str | None] = mapped_column(String(40))
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    pages_done: Mapped[int] = mapped_column(Integer, default=0)
    ocr_pages: Mapped[int] = mapped_column(Integer, default=0)
    ocr_seconds: Mapped[float] = mapped_column(Numeric(16, 3), default=0)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_political_report_external"),)


class PoliticalPage(Base):
    __tablename__ = "political_page"
    report_id: Mapped[str] = mapped_column(ForeignKey("political_report.id"), primary_key=True)
    page: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    ocr: Mapped[bool] = mapped_column(default=False)
    seconds: Mapped[float] = mapped_column(Numeric(16, 3), default=0)
    raw_text: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class PoliticalTransaction(Base):
    __tablename__ = "political_transaction"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    report_id: Mapped[str] = mapped_column(ForeignKey("political_report.id"), index=True)
    page: Mapped[int] = mapped_column(Integer)
    row_number: Mapped[int] = mapped_column(Integer)
    owner: Mapped[str] = mapped_column(String(16), default="unknown")
    asset_name: Mapped[str] = mapped_column(Text)
    ticker: Mapped[str | None] = mapped_column(String(24), index=True)
    ticker_origin: Mapped[str | None] = mapped_column(String(16))
    issuer_ticker: Mapped[str | None] = mapped_column(String(24))
    issuer_ticker_origin: Mapped[str | None] = mapped_column(String(16))
    asset_type: Mapped[str] = mapped_column(String(24), default="unknown")
    option_type: Mapped[str | None] = mapped_column(String(8))
    strike_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    expiration_date: Mapped[date | None] = mapped_column(Date)
    transaction_type: Mapped[str] = mapped_column(String(24), default="unknown")
    transaction_date: Mapped[date | None] = mapped_column(Date, index=True)
    notification_date: Mapped[date | None] = mapped_column(Date)
    amount_low: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    amount_high: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    raw_row: Mapped[str] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(String(16), default="review")
    warnings: Mapped[list] = mapped_column(JSONB, default=list)
    __table_args__ = (
        UniqueConstraint("report_id", "page", "row_number", name="uq_political_transaction_row"),
        Index("ix_political_transaction_direction_date", "transaction_type", "transaction_date"),
    )
