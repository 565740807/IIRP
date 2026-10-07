"""SEC filings, row-level insider transactions and the feed's revisions and reading sessions."""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
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

from iirp.models.base import Base, now, uid


class Issuer(Base):
    __tablename__ = "issuer"
    id: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    # Ticker stated by the most recently accepted filing (lookup only).
    ticker: Mapped[str | None] = mapped_column(String(32), index=True)


class Owner(Base):
    __tablename__ = "reporting_owner"
    id: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str] = mapped_column(Text)


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
    source_hash: Mapped[str] = mapped_column(ForeignKey("source_object.sha256"), index=True)
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


class FeedSession(Base):
    __tablename__ = "feed_session"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    revision_ids: Mapped[list] = mapped_column(JSONB)
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
