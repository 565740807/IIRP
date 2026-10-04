"""Immutable event imports and revisions; prices/results use existing research tables."""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from iirp.models import Base, now


def uid():
    import uuid

    return str(uuid.uuid4())


class EventImportPreview(Base):
    __tablename__ = "event_import_preview"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    raw_text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    warnings: Mapped[list] = mapped_column(JSONB)
    set_id: Mapped[str | None] = mapped_column(ForeignKey("event_set.id"))
    expected_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class EventSet(Base):
    __tablename__ = "event_set"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    security_id: Mapped[str] = mapped_column(ForeignKey("security.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class EventSetVersion(Base):
    __tablename__ = "event_set_version"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    set_id: Mapped[str] = mapped_column(ForeignKey("event_set.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    preview_id: Mapped[str] = mapped_column(ForeignKey("event_import_preview.id"))
    content_hash: Mapped[str] = mapped_column(String(64))
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    events: Mapped[list] = mapped_column(JSONB)
    reviews: Mapped[list] = mapped_column(JSONB)
    revision_note: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("set_id", "version"),)


class EventCommandReceipt(Base):
    __tablename__ = "event_command_receipt"
    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
