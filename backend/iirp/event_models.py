"""Saved event sets and prompt templates: user data, kept until deleted (D14)."""

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from iirp.models import Base, now


def uid():
    import uuid

    return str(uuid.uuid4())


class EventSet(Base):
    """One pasted event list (earnings or custom), possibly for several tickers."""

    __tablename__ = "event_set"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(200))
    # Normalized items of iirp.event_input, sorted by date.
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

