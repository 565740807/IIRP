import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    literal_column,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


ACTIVE = ("QUEUED", "RUNNING", "PAUSE_REQUESTED", "PAUSED", "CANCEL_REQUESTED", "RETRY_WAIT")
TERMINAL = ("SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED")
# Partial-index predicate of ix_job_sec_latest_complete (migration 0023).
SEC_LATEST_COMPLETE = (
    "kind = 'sec_discover' AND status = 'SUCCEEDED' AND (target ->> 'mode') = 'latest' "
    "AND (checkpoint['sec_scan'] ->> 'complete') = 'true'"
)


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
            "ix_job_sec_latest_complete",
            created_at.desc(),
            postgresql_where=text(SEC_LATEST_COMPLETE),
        ),
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


def latest_complete_sec_scan(before=None):
    """Newest succeeded latest-feed scan that reached its watermark.

    Its conditions match SEC_LATEST_COMPLETE, so PostgreSQL answers from the
    partial index even while no scan has completed yet (then nothing matches).
    """
    query = select(Job).where(
        Job.kind == "sec_discover",
        Job.status == "SUCCEEDED",
        Job.target["mode"].astext == "latest",
        Job.checkpoint["sec_scan"]["complete"].astext == "true",
    )
    if before is not None:
        query = query.where(Job.created_at < before)
    return query.order_by(Job.created_at.desc()).limit(1)


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


class Coverage(Base):
    __tablename__ = "coverage"
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    target: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    source_hash: Mapped[str | None] = mapped_column(ForeignKey("source_object.sha256"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SourceBudget(Base):
    __tablename__ = "source_budget"
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    next_allowed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    failures: Mapped[int] = mapped_column(Integer, default=0)


# Register the incremental business tables on the same migration metadata.
from iirp import business_models as business_models  # noqa: E402
from iirp import event_models as event_models  # noqa: E402
from iirp import research_tracking as _research_tracking  # noqa: E402,F401
