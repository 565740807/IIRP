"""Notifications about followed people's trades and rule matches, and what was pushed to a phone."""
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from iirp.models.base import Base, now, uid


class Notification(Base):
    """One notification. ``dedupe_key`` names what it is about, so a filing or report is
    notified once however often it is read again: ``trade:sec:<accession>``,
    ``trade:political:<report id>``, ``rule_executive:<accession>`` or
    ``rule_cluster:<issuer>:<first trade day of the window>``."""
    __tablename__ = "notification"
    __table_args__ = (Index("ix_notification_created", "created_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    dedupe_key: Mapped[str] = mapped_column(Text, unique=True)
    # trade, rule_executive or rule_cluster.
    kind: Mapped[str] = mapped_column(String(24))
    person_key: Mapped[str | None] = mapped_column(Text)
    issuer_id: Mapped[str | None] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    # Public facts only: who, which stock, buy or sell, amount, trade and publication
    # dates, and the original's public link.
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # For the phone: None (push was off), pending, sent, failed.
    push_state: Mapped[str | None] = mapped_column(String(16), index=True)


class NotificationPush(Base):
    """One message sent (or attempted) to the push server: a single notification, a digest of
    several from the same minute, or a test."""
    __tablename__ = "notification_push"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    # single, digest or test.
    kind: Mapped[str] = mapped_column(String(16))
    # pending (a test waiting to go), sent or failed.
    status: Mapped[str] = mapped_column(String(16), index=True)
    count: Mapped[int] = mapped_column(Integer, default=1)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
