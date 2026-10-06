"""Durable jobs, batches (one per user demand), schedules and preferences."""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    literal_column,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from iirp.models.base import Base, now, uid


class Job(Base):
    __tablename__ = "job"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(160))
    target: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="QUEUED")
    trigger: Mapped[str] = mapped_column(String(16), default="manual")
    priority: Mapped[int] = mapped_column(Integer, default=10)
    requested_action: Mapped[str | None] = mapped_column(String(16))
    control_version: Mapped[int] = mapped_column(Integer, default=0)
    progress_done: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=1)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index("ix_job_history_key", idempotency_key, created_at.desc()),
        Index(
            "ix_job_sec_document_accession_latest",
            literal_column("(target ->> 'accession'::text)"),
            created_at.desc(), id.desc(),
            postgresql_where=text("kind = 'sec_document'"),
        ),
        Index("ix_job_kind_status_finished", "kind", "status", "finished_at"),
        Index("ix_job_created_id", created_at.desc(), id.desc()),
        Index(
            "ix_job_sec_claim_stamp",
            "kind",
            literal_column("(checkpoint ->> '_queue_sec_claimed_at'::text)").desc(),
            postgresql_where=text(
                "((checkpoint ->> '_queue_sec_claimed_at'::text) IS NOT NULL)"
            ),
        ),
        Index(
            "uq_active_job_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text(
                "status IN ('QUEUED','RUNNING','PAUSE_REQUESTED','PAUSED','CANCEL_REQUESTED','RETRY_WAIT')"
            ),
        ),
        Index("ix_job_claim", "status", "available_at", "priority"),
        CheckConstraint(
            "progress_done >= 0 AND progress_done <= progress_total", name="ck_progress"
        ),
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','SUCCEEDED','PARTIAL','FAILED','PAUSE_REQUESTED','PAUSED','CANCEL_REQUESTED','CANCELLED','RETRY_WAIT')",
            name="ck_job_status",
        ),
    )


class Subscription(Base):
    __tablename__ = "job_subscription"
    job_id: Mapped[str] = mapped_column(ForeignKey("job.id"), primary_key=True)
    source: Mapped[str] = mapped_column(String(16), primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Policy(Base):
    __tablename__ = "collection_policy"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    sec_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeat"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Batch(Base):
    __tablename__ = "batch"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    request_id: Mapped[str] = mapped_column(String(128), unique=True)
    scope_key: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(200))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    trigger: Mapped[str] = mapped_column(String(16), default="manual")
    policy_key: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24), default="QUEUED", index=True)
    requested_action: Mapped[str | None] = mapped_column(String(16))
    control_version: Mapped[int] = mapped_column(Integer, default=0)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("batch.id"))
    last_planned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    planning_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    planning_error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    planning_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class BatchPlanSignal(Base):
    """One durable, coalesced planning notification per demand."""
    __tablename__ = "batch_plan_signal"
    batch_id: Mapped[str] = mapped_column(ForeignKey("batch.id"), primary_key=True)
    token: Mapped[str] = mapped_column(String(36))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class RequestScope(Base):
    __tablename__ = "request_scope"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batch.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    security_id: Mapped[str | None] = mapped_column(ForeignKey("security.id"), index=True)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(32), default="QUEUED")
    wait_reason: Mapped[str | None] = mapped_column(Text)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    __table_args__ = (UniqueConstraint("batch_id", "symbol"),)


class RequestReceipt(Base):
    __tablename__ = "request_receipt"
    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batch.id"))
    scope_key: Mapped[str] = mapped_column(String(64))


class BatchJob(Base):
    __tablename__ = "batch_job"
    __table_args__ = (Index("ix_batch_job_job_active", "job_id", "active"),)
    scope_id: Mapped[str] = mapped_column(ForeignKey("request_scope.id"), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("job.id"), primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class JobDependency(Base):
    __tablename__ = "job_dependency"
    job_id: Mapped[str] = mapped_column(ForeignKey("job.id"), primary_key=True)
    prerequisite_id: Mapped[str] = mapped_column(ForeignKey("job.id"), primary_key=True)


class CollectionStrategy(Base):
    __tablename__ = "collection_strategy"
    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Preferences(Base):
    __tablename__ = "user_preferences"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    values: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)


class MaintenanceRun(Base):
    __tablename__ = "maintenance_run"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
