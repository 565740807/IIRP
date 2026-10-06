"""Raw source files, observations, provider budgets and SEC polling state."""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from iirp.models.base import Base, now, uid


class SourceObject(Base):
    __tablename__ = "source_object"
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    relative_path: Mapped[str] = mapped_column(Text, unique=True)
    byte_size: Mapped[int] = mapped_column(Integer)
    media_type: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    # Refetchable discovery sources (list pages, index files, response JSON)
    # expire; NULL is permanent. A permanent reference to the same content wins.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index("ix_source_object_expires_at", "expires_at",
              postgresql_where=text("expires_at IS NOT NULL")),
    )


class SourceObservation(Base):
    __tablename__ = "source_observation"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("job.id"), index=True)
    source_hash: Mapped[str] = mapped_column(ForeignKey("source_object.sha256"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SourcePoll(Base):
    """One row per polled source: position, cadence and lease, no job per poll."""

    __tablename__ = "source_poll"
    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    watermark_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    catchup: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    next_poll_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_complete_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    last_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SourceBudget(Base):
    __tablename__ = "source_budget"
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    next_allowed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    failures: Mapped[int] = mapped_column(Integer, default=0)


class Coverage(Base):
    __tablename__ = "coverage"
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    target: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    source_hash: Mapped[str | None] = mapped_column(ForeignKey("source_object.sha256"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CoverageSegment(Base):
    __tablename__ = "coverage_segment"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    security_id: Mapped[str | None] = mapped_column(ForeignKey("security.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(32))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    source_hash: Mapped[str | None] = mapped_column(ForeignKey("source_object.sha256"))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
