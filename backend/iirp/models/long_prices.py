"""Re-downloadable daily history and durable membership/retention controls."""
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from iirp.models.base import Base, now, uid


class LongPriceSeries(Base):
    __tablename__ = "long_price_series"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    symbol: Mapped[str] = mapped_column(String(32), unique=True)
    security_id: Mapped[str] = mapped_column(ForeignKey("security.id"), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    removed_on: Mapped[date | None] = mapped_column(Date)
    retain_from: Mapped[date] = mapped_column(Date)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    needs_full: Mapped[bool] = mapped_column(Boolean, default=True)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    complete_through: Mapped[date | None] = mapped_column(Date)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    @property
    def expires_at(self):
        # Research still expires after 24 hours; daily history itself does not.
        return now() + timedelta(hours=24)


class LongPriceBar(Base):
    __tablename__ = "long_price_bar"
    cache_id: Mapped[str] = mapped_column(ForeignKey("long_price_series.id", ondelete="CASCADE"), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Any] = mapped_column(Numeric(30, 12))
    high: Mapped[Any] = mapped_column(Numeric(30, 12))
    low: Mapped[Any] = mapped_column(Numeric(30, 12))
    close: Mapped[Any] = mapped_column(Numeric(30, 12))
    volume: Mapped[Any | None] = mapped_column(Numeric(32, 4))
    dividends: Mapped[Any | None] = mapped_column(Numeric(30, 12))
    splits: Mapped[Any | None] = mapped_column(Numeric(30, 12))
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)

    @property
    def adj_close(self):
        return None
