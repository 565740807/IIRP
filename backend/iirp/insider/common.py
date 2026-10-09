"""Small helpers shared by the insider modules: identities, dates and date anomalies."""

import base64
import uuid
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, cast, func

from iirp.messages import UserError, msg
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
        raise ValueError("acceptance time must carry a real time and time zone")
    return parsed


def _date(value) -> date | None:
    if value == "":
        return None
    return date.fromisoformat(value) if isinstance(value, str) else value


DATE_ANOMALY_LABEL = msg("insider.date_anomaly")
ACTION_CATEGORIES = ("purchase_market_or_private", "sale_market_or_private", "grant_or_award",
                     "tax_or_exercise_price_withholding", "exercise_or_conversion",
                     "needs_review", "other")


def transaction_kind(category, table) -> str:
    """Display label of a row: its action category, marked when it is a derivative."""
    kind = msg("insider.action." + category) if category in ACTION_CATEGORIES else category
    return msg("insider.kind.derivative", kind=kind) if table == "II" else kind


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
        raise UserError("insider.cik_invalid")
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
