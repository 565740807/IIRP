"""Analysis requests and results, exports, saved event sets and prompt templates."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from iirp.models.base import Base, now, uid
from iirp.models.compressed import CompressedResultJSON


class AnalysisRequest(Base):
    __tablename__ = "analysis_request"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batch.id"), index=True)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AnalysisResult(Base):
    __tablename__ = "analysis_result"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_request.id"), index=True)
    security_id: Mapped[str] = mapped_column(ForeignKey("security.id"))
    input_key: Mapped[str] = mapped_column(String(64))
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB)
    legacy_data: Mapped[dict[str, Any]] = mapped_column("data", JSONB, default=dict)
    payload: Mapped[dict[str, Any] | None] = mapped_column(CompressedResultJSON())
    overlap_projection: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    accessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    # The earliest expiry of the price caches it was computed from (D14).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    __table_args__ = (
        UniqueConstraint("analysis_id", "security_id", "input_key"),
        Index("ix_analysis_result_shared_input", "security_id", "input_key", "created_at"),
        CheckConstraint(
            "(payload IS NULL AND overlap_projection IS NULL) OR "
            "(payload IS NOT NULL AND overlap_projection IS NOT NULL AND data='{}'::jsonb)",
            name="ck_analysis_result_projection_pair",
        ),
    )

    @property
    def data(self):
        return self.payload if self.payload is not None else self.legacy_data

    @data.setter
    def data(self, value):
        # Compressed payloads were only used by removed SEC earnings results.
        self.overlap_projection = None
        self.payload = None
        self.legacy_data = value


class ExportManifest(Base):
    __tablename__ = "export_manifest"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    result_ids: Mapped[list] = mapped_column(JSONB)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ResearchTrack(Base):
    __tablename__ = "research_track"
    origin_id: Mapped[str] = mapped_column(ForeignKey("analysis_request.id"), primary_key=True)
    latest_id: Mapped[str] = mapped_column(ForeignKey("analysis_request.id"), index=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class EventSet(Base):
    """One pasted event list (earnings or custom), possibly for several tickers."""

    __tablename__ = "event_set"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(200))
    # Normalized items of iirp.events.input, sorted by date.
    events: Mapped[list] = mapped_column(JSONB)
    request_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PromptTemplate(Base):
    """A user-edited prompt; the built-in default applies when no row exists."""

    __tablename__ = "prompt_template"
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    language: Mapped[str] = mapped_column(String(8), primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
