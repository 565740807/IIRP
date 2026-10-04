"""A mutable latest pointer; immutable analysis requests/results remain untouched."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from iirp.models import Base, now


class ResearchTrack(Base):
    __tablename__ = "research_track"
    origin_id: Mapped[str] = mapped_column(ForeignKey("analysis_request.id"), primary_key=True)
    latest_id: Mapped[str] = mapped_column(ForeignKey("analysis_request.id"), index=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
