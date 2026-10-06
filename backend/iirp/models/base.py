"""Declarative base and helpers shared by every table."""

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import DeclarativeBase


def now():
    return datetime.now(timezone.utc)


def uid():
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


ACTIVE = ("QUEUED", "RUNNING", "PAUSE_REQUESTED", "PAUSED", "CANCEL_REQUESTED", "RETRY_WAIT")


TERMINAL = ("SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED")
