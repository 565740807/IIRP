"""Research facts, immutable observations and independently controlled demands."""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql.base import ischema_names
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

from iirp.models import Base, now
from iirp.result_storage import CompressedResultJSON


def uid():
    return str(uuid.uuid4())


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


class Issuer(Base):
    __tablename__ = "issuer"
    id: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str] = mapped_column(Text)


class Owner(Base):
    __tablename__ = "reporting_owner"
    id: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str] = mapped_column(Text)


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


class SourceObservation(Base):
    __tablename__ = "source_observation"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("job.id"), index=True)
    source_hash: Mapped[str] = mapped_column(ForeignKey("source_object.sha256"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MarketQuote(Base):
    __tablename__ = "market_quote_cache"
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Filing(Base):
    __tablename__ = "filing"
    accession: Mapped[str] = mapped_column(String(20), primary_key=True)
    form: Mapped[str] = mapped_column(String(8))
    filing_date: Mapped[date | None] = mapped_column(Date)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    issuer_id: Mapped[str | None] = mapped_column(ForeignKey("issuer.id"), index=True)
    index_url: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="DISCOVERED")
    current_version: Mapped[str | None] = mapped_column(String(36))
    visible: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (
        Index("ix_filing_pending", accession,
              postgresql_where=text("visible IS TRUE AND current_version IS NULL")),
    )


class FilingVersion(Base):
    __tablename__ = "filing_version"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    accession: Mapped[str] = mapped_column(ForeignKey("filing.accession"), index=True)
    source_hash: Mapped[str] = mapped_column(ForeignKey("source_object.sha256"))
    parser_version: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("accession", "source_hash", "parser_version"),)


class FilingOwner(Base):
    __tablename__ = "filing_owner"
    version_id: Mapped[str] = mapped_column(ForeignKey("filing_version.id"), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("reporting_owner.id"), primary_key=True)
    relationship: Mapped[dict[str, Any]] = mapped_column(JSONB)


class TransactionEvent(Base):
    __tablename__ = "transaction_event"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    issuer_id: Mapped[str] = mapped_column(ForeignKey("issuer.id"), index=True)
    accession: Mapped[str] = mapped_column(ForeignKey("filing.accession"), index=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("filing_version.id"))
    row_key: Mapped[str] = mapped_column(String(100))
    transaction_date: Mapped[date | None] = mapped_column(Date, index=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    owner_ids: Mapped[list] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(32), default="CURRENT")
    replaces_id: Mapped[str | None] = mapped_column(ForeignKey("transaction_event.id"))
    __table_args__ = (
        UniqueConstraint("version_id", "row_key"),
        Index("ix_event_issuer_date", "issuer_id", "transaction_date", "id"),
        Index("ix_event_recent_transaction_date", transaction_date.desc().nulls_last(), id.desc()),
        Index("ix_event_recent_accepted_at", accepted_at.desc().nulls_last(), id.desc()),
    )


class AmendmentRelation(Base):
    __tablename__ = "amendment_relation"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    accession: Mapped[str] = mapped_column(ForeignKey("filing.accession"))
    original_event_id: Mapped[str | None] = mapped_column(ForeignKey("transaction_event.id"))
    amended_event_id: Mapped[str | None] = mapped_column(ForeignKey("transaction_event.id"))
    action: Mapped[str] = mapped_column(String(24), default="UNCONFIRMED")
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class XID8(UserDefinedType):
    """PostgreSQL 64-bit transaction id; compared only inside SQL."""
    cache_ok = True

    def get_col_spec(self, **kw):
        return "xid8"


class PGSnapshot(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **kw):
        return "pg_snapshot"


# Let schema reflection (migration checks) recognise these PostgreSQL types.
ischema_names.setdefault("xid8", XID8)
ischema_names.setdefault("pg_snapshot", PGSnapshot)

FEED_REVISION_SEQ = Sequence("feed_revision_seq", metadata=Base.metadata)


class FeedRevision(Base):
    __tablename__ = "feed_group_revision"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    group_key: Mapped[str] = mapped_column(String(32), index=True)
    issuer_id: Mapped[str] = mapped_column(ForeignKey("issuer.id"))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    match_kinds: Mapped[list[str]] = mapped_column(JSONB, default=list)
    transaction_sort_dates: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    # Reading watermarks: publication order and the writing transaction. Rows
    # written before the watermark schema have NULLs and are visible to all.
    seq: Mapped[int | None] = mapped_column(BigInteger, server_default=FEED_REVISION_SEQ.next_value())
    xid: Mapped[str | None] = mapped_column(XID8(), server_default=text("pg_current_xact_id()"))
    __table_args__ = (
        Index("ix_feed_group_revision_seq", "seq", postgresql_where=text("seq IS NOT NULL")),
        Index("ix_feed_group_revision_xid", "xid", postgresql_where=text("xid IS NOT NULL")),
    )


class FeedGroupCurrent(Base):
    """One pointer per company/date group to its newest revision."""
    __tablename__ = "feed_group_current"
    group_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("feed_group_revision.id"))
    issuer_id: Mapped[str] = mapped_column(String(10))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    row_count: Mapped[int] = mapped_column(Integer)
    seq: Mapped[int | None] = mapped_column(BigInteger)
    xid: Mapped[str | None] = mapped_column(XID8())
    __table_args__ = (
        Index("ix_feed_group_current_seq", "seq", postgresql_where=text("seq IS NOT NULL")),
        Index("ix_feed_group_current_xid", "xid", postgresql_where=text("xid IS NOT NULL")),
    )


class FeedWatermarkCluster(Base):
    """The cluster whose transaction ids are stored in revision watermarks."""
    __tablename__ = "feed_watermark_cluster"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    system_identifier: Mapped[str] = mapped_column(Text)


class FeedGroupOrder(Base):
    """Sort keys of each current non-empty revision, one row per filter and order."""
    __tablename__ = "feed_group_order"
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    sort_order: Mapped[str] = mapped_column(String(16), primary_key=True)
    group_key: Mapped[str] = mapped_column(String(32, collation="C"), primary_key=True)
    sort_key: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revision_id: Mapped[str] = mapped_column(String(36))
    seq: Mapped[int | None] = mapped_column(BigInteger)
    xid: Mapped[str | None] = mapped_column(XID8())
    __table_args__ = (
        Index("ix_feed_group_order_page", "kind", "sort_order", sort_key.desc(),
              accepted_at.desc(), group_key.desc()),
    )


class FeedManifest(Base):
    __tablename__ = "feed_manifest"
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision_ids: Mapped[list] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (Index("ix_feed_manifest_created", created_at, sha256),)


class FeedSession(Base):
    __tablename__ = "feed_session"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    revision_ids: Mapped[list] = mapped_column(JSONB)
    manifest_hash: Mapped[str | None] = mapped_column(ForeignKey("feed_manifest.sha256"), index=True)
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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


class MaintenanceRun(Base):
    __tablename__ = "maintenance_run"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
