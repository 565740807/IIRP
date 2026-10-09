"""Securities, the 24-hour daily price cache (D14) and home-page quotes."""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from iirp.models.base import Base, now, uid


class Security(Base):
    __tablename__ = "security"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(Text, default="")
    issuer_id: Mapped[str | None] = mapped_column(ForeignKey("issuer.id"), index=True)
    instrument: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    currency: Mapped[str | None] = mapped_column(String(16))
    exchange: Mapped[str | None] = mapped_column(String(32))
    calendar: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class SecurityIdentifier(Base):
    __tablename__ = "security_identifier"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    security_id: Mapped[str] = mapped_column(ForeignKey("security.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    __table_args__ = (UniqueConstraint("provider", "symbol", "valid_from"),)


class PriceCache(Base):
    """One provider response per security, valid for 24 hours (D14).

    A single request covers the whole needed range, so all bars share one
    adjustment basis; nothing is versioned or stitched. When a new need falls
    outside the cached range, the security is fetched again as one wider range
    and this row is replaced. Expired rows are deleted with their results.
    """
    __tablename__ = "price_cache"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    security_id: Mapped[str] = mapped_column(ForeignKey("security.id"), unique=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    # Last completed exchange session when fetched; later sessions are unknown.
    complete_through: Mapped[date] = mapped_column(Date)
    provider: Mapped[str] = mapped_column(String(32), default="yfinance")
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class PriceCacheBar(Base):
    __tablename__ = "price_cache_bar"
    cache_id: Mapped[str] = mapped_column(
        ForeignKey("price_cache.id", ondelete="CASCADE"), primary_key=True
    )
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Any | None] = mapped_column(Numeric(30, 12))
    high: Mapped[Any | None] = mapped_column(Numeric(30, 12))
    low: Mapped[Any | None] = mapped_column(Numeric(30, 12))
    close: Mapped[Any | None] = mapped_column(Numeric(30, 12))
    adj_close: Mapped[Any | None] = mapped_column(Numeric(30, 12))
    volume: Mapped[Any | None] = mapped_column(Numeric(32, 4))
    dividends: Mapped[Any | None] = mapped_column(Numeric(30, 12))
    splits: Mapped[Any | None] = mapped_column(Numeric(30, 12))
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)


class MarketQuote(Base):
    __tablename__ = "market_quote_cache"
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class IndexConstituent(Base):
    __tablename__ = "index_constituent"
    index_name: Mapped[str] = mapped_column(String(16), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(32), primary_key=True)
    cik: Mapped[str | None] = mapped_column(String(10), index=True)
    name: Mapped[str] = mapped_column(Text)
    industry: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class IndexConstituentState(Base):
    __tablename__ = "index_constituent_state"
    index_name: Mapped[str] = mapped_column(String(16), primary_key=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
