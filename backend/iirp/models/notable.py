"""The people the user follows: SEC filers, House members and OGE filers."""
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from iirp.models.base import Base, now


class NotablePerson(Base):
    """One followed person by a stable key (``iirp.notable.people``), never a generated row id:
    ``sec:<10-digit CIK>``, ``house:<name>|<district>`` or ``oge:<name>|<position>``."""
    __tablename__ = "notable_person"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    source: Mapped[str] = mapped_column(String(8))
    # The name shown when no local record names the person.
    name: Mapped[str] = mapped_column(Text)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
