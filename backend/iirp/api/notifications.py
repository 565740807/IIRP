"""Notifications: the bell's list and unread count, marking read, the settings (rules,
phone pushes through ntfy) and a test push. Reads and writes the local database only;
the worker sends pushes."""

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from iirp.api.insider_schemas import Output
from iirp.db import session
from iirp.messages import UserError
from iirp.models import now
from iirp.models.notifications import Notification

router = APIRouter(prefix="/api/v1/notifications")


class NotificationItem(Output):
    id: str
    kind: Literal["trade", "rule_executive", "rule_cluster"]
    person_key: str | None
    issuer_id: str | None
    created_at: datetime
    read: bool
    # Public facts as notified: source, person, ticker, asset_name, buy / sell parts or report
    # rows, amounts, traded_from / traded_to, published, url, amendment, trade_id.
    data: dict[str, Any]


class NotificationsOutput(Output):
    items: list[NotificationItem]
    unread: int
    enabled: bool
    # Older notifications exist before the last item.
    more: bool


class ReadInput(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=200)
    # Mark every notification read.
    all: bool = False


class RuleSettings(Output):
    enabled: bool
    scope: Literal["index_watchlist", "all"]
    executive_min: int
    cluster_people: int
    cluster_days: int
    exclude_plans: bool


class NtfySettings(Output):
    enabled: bool
    server: str
    topic: str | None
    language: Literal["en", "zh"]


class PushStatus(Output):
    last_status: Literal["sent", "failed"] | None
    last_kind: Literal["single", "digest", "test"] | None
    last_at: datetime | None
    last_error: str | None
    sent_today: int
    daily_limit: int
    waiting: int


class NotificationSettingsOutput(Output):
    enabled: bool
    enabled_at: datetime | None
    rules: RuleSettings
    ntfy: NtfySettings
    push: PushStatus


class RuleChange(BaseModel):
    enabled: bool | None = None
    scope: Literal["index_watchlist", "all"] | None = None
    executive_min: int | None = None
    cluster_people: int | None = None
    cluster_days: int | None = None
    exclude_plans: bool | None = None


class NtfyChange(BaseModel):
    enabled: bool | None = None
    server: str | None = Field(default=None, max_length=200)
    language: Literal["en", "zh"] | None = None


class SettingsChange(BaseModel):
    enabled: bool | None = None
    rules: RuleChange | None = None
    ntfy: NtfyChange | None = None


def _settings(s):
    from iirp.notifications import push, settings

    current = settings.load(s)
    return {**current, "push": push.status(s)}


def _list(s, limit, before):
    from iirp.notifications.settings import load

    query = select(Notification).order_by(Notification.created_at.desc(), Notification.id.desc())
    if before:
        query = query.where(Notification.created_at < before)
    rows = s.scalars(query.limit(limit + 1)).all()
    unread = s.scalar(select(func.count()).select_from(Notification).where(Notification.read_at.is_(None)))
    return {"items": [{"id": row.id, "kind": row.kind, "person_key": row.person_key, "issuer_id": row.issuer_id,
                       "created_at": row.created_at, "read": row.read_at is not None, "data": row.data}
                      for row in rows[:limit]],
            "unread": unread, "enabled": load(s)["enabled"], "more": len(rows) > limit}


@router.get("", response_model=NotificationsOutput)
def read_notifications(limit: int = Query(default=30, ge=1, le=100), before: datetime | None = None):
    """Newest first, with the unread count."""
    with session() as s:
        return _list(s, limit, before)


@router.post("/read", response_model=NotificationsOutput)
def mark_read(body: ReadInput):
    with session() as s, s.begin():
        query = update(Notification).where(Notification.read_at.is_(None))
        if not body.all:
            query = query.where(Notification.id.in_(body.ids or [""]))
        s.execute(query.values(read_at=now()))
        return _list(s, 30, None)


@router.get("/settings", response_model=NotificationSettingsOutput)
def read_settings():
    with session() as s:
        return _settings(s)


@router.put("/settings", response_model=NotificationSettingsOutput)
def change_settings(body: SettingsChange):
    from iirp.notifications.settings import update as save

    try:
        with session() as s, s.begin():
            save(s, body.model_dump(exclude_none=True))
            return _settings(s)
    except UserError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/settings/topic", response_model=NotificationSettingsOutput)
def new_topic():
    """A new random topic; phones subscribed to the old one stop receiving."""
    from iirp.notifications.settings import regenerate_topic

    with session() as s, s.begin():
        regenerate_topic(s)
        return _settings(s)


@router.post("/test", response_model=NotificationSettingsOutput, status_code=202)
def send_test():
    """Queue a test push; the worker sends it within seconds."""
    from iirp.notifications.push import queue_test

    try:
        with session() as s, s.begin():
            queue_test(s)
            return _settings(s)
    except UserError as exc:
        raise HTTPException(409, str(exc)) from exc

