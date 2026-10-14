"""The user's watched stock codes."""
from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from iirp.models.base import Base, now


class WatchlistItem(Base):
    """One watched code in its compared form (``iirp.watchlist.ticker_key``) and when it was added."""
    __tablename__ = "watchlist_item"
    ticker: Mapped[str] = mapped_column(String(24), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
