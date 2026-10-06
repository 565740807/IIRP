"""Small helpers shared by the insider modules: identities, dates and date anomalies."""

import base64
import uuid
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, cast, func

from iirp.models import (
    TransactionEvent,
)

PARSER_VERSION = "ownership-v1.1"


ET = ZoneInfo("America/New_York")


VISIBLE = ("CURRENT", "NEEDS_REVIEW")


_NAMESPACE = uuid.UUID("9916b579-ef13-4d36-aeb5-4bcb5e572f11")


def _uid(*parts) -> str:
    return str(uuid.uuid5(_NAMESPACE, ":".join(str(part) for part in parts)))


def _json(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return {"encoding": "base64", "payload": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {key: _json(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(child) for child in value]
    return value


def _instant(value) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise ValueError("接受时间必须包含真实时间和时区。")
    return parsed


def _date(value) -> date | None:
    if value == "":
        return None
    return date.fromisoformat(value) if isinstance(value, str) else value


DATE_ANOMALY_LABEL = "日期异常、待核对"


def _accepted_day(value) -> str | None:
    instant = _instant(value)
    return instant.astimezone(ET).date().isoformat() if instant else None


def _date_anomaly(row) -> dict | None:
    """A transaction dated after its own SEC acceptance date, e.g. a mistyped year.

    The reported value is kept and shown; it never drives date sorting or ranges.
    """
    transaction, accepted = row.get("transaction_date"), _accepted_day(row.get("accepted_at"))
    if transaction and accepted and str(transaction) > accepted:
        return {"code": "TRANSACTION_AFTER_ACCEPTANCE", "label": DATE_ANOMALY_LABEL,
                "transaction_date": str(transaction), "accepted_date": accepted}
    return None


def _sort_transaction_date(row) -> str:
    return "" if _date_anomaly(row) else (row.get("transaction_date") or "")


def _cik(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdigit()
        or not 1 <= len(value) <= 10
        or int(value) == 0
    ):
        raise ValueError("CIK 必须是 1 至 10 位数字。")
    return value.zfill(10)


def _group_date(event) -> date | None:
    accepted = _instant(event.data.get("original_accepted_at")) or event.accepted_at
    if not accepted:
        return None
    return accepted.astimezone(ET).date()


def _group_time_column():
    return func.coalesce(
        cast(TransactionEvent.data["original_accepted_at"].astext, DateTime(timezone=True)),
        TransactionEvent.accepted_at,
    )
