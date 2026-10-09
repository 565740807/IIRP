"""The short event JSON pasted back from an external AI.

    {"events": [{"ticker": "AAPL", "date": "2025-10-30", "session": "after_close",
                 "name": "FY2025 Q4 earnings", "fiscal_year": 2025, "fiscal_quarter": 4,
                 "note": "optional"}]}

Validation names the item (1-based), the field and the reason, so a user can
fix the text by hand or ask the AI again. Nothing is fetched or verified here.
"""

import json
import re
from datetime import date

from iirp.messages import UserError, msg

KINDS = ("earnings", "custom")
SESSIONS = ("before_open", "during", "after_close", "unknown")
FIELDS = ("ticker", "date", "session", "name", "fiscal_year", "fiscal_quarter", "note")
MAX_EVENTS = 1000
MAX_TEXT = 512 * 1024
TICKER = re.compile(r"^[A-Z0-9^][A-Z0-9.^=\-]{0,19}$")


class EventInputError(ValueError):
    """Carries every problem found, each as {"index", "field", "message"}."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__(msg("events.input.issues", issues=[_describe(e) for e in errors[:5]]))


def _describe(error):
    code = "events.input.issue_item" if error.get("index") else "events.input.issue_whole"
    if error.get("field"):
        code += "_field"
    return msg(code, index=error.get("index"), field=error.get("field"), message=error["message"])


def _json(text):
    text = text.strip()
    fenced = re.fullmatch(r"```[a-zA-Z]*\s*\n([\s\S]*?)\n?```", text)
    if fenced:
        text = fenced.group(1).strip()

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise UserError("events.input.duplicate_key", key=key)
            result[key] = value
        return result

    def constant(value):
        raise UserError("events.input.json_constant", value=value)

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except json.JSONDecodeError as exc:
        raise EventInputError([{"index": None, "field": None,
            "message": msg("events.input.invalid_json", line=exc.lineno, column=exc.colno, reason=exc.msg)}]) from None
    except ValueError as exc:
        raise EventInputError([{"index": None, "field": None, "message": str(exc)}]) from None


def _int(value):
    return value if type(value) is int else None


def _event(index, item, kind, errors):
    def error(field, message):
        errors.append({"index": index, "field": field, "message": message})

    if not isinstance(item, dict):
        error(None, msg("events.input.item_not_object"))
        return None
    for key in item:
        if key not in FIELDS:
            error(key, msg("events.input.unknown_field", fields=", ".join(FIELDS)))
    ticker = item.get("ticker")
    if not isinstance(ticker, str) or not ticker.strip():
        error("ticker", msg("events.input.ticker_missing"))
    elif not TICKER.fullmatch(ticker.strip().upper()):
        error("ticker", msg("events.input.ticker_invalid", value=repr(ticker)))
    day = item.get("date")
    parsed = None
    if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        error("date", msg("events.input.date_format", value=repr(day)))
    else:
        try:
            parsed = date.fromisoformat(day)
        except ValueError:
            error("date", msg("events.input.date_invalid", value=day))
        if parsed and parsed.year < 1990:
            error("date", msg("events.input.date_too_early"))
    session = item.get("session")
    if session not in SESSIONS:
        error("session", msg("events.input.session_invalid", sessions=" / ".join(SESSIONS),
                               value=repr(session)))
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        error("name", msg("events.input.name_missing"))
    elif len(name) > 200:
        error("name", msg("events.input.name_too_long", max=200))
    year, quarter = item.get("fiscal_year"), item.get("fiscal_quarter")
    if kind == "earnings" or year is not None or quarter is not None:
        if _int(year) is None or not 1990 <= year <= 2100:
            error("fiscal_year", msg("events.input.fiscal_year_required" if kind == "earnings"
                                     else "events.input.fiscal_year_invalid"))
        if _int(quarter) is None or not 1 <= quarter <= 4:
            error("fiscal_quarter", msg("events.input.fiscal_quarter_required" if kind == "earnings"
                                        else "events.input.fiscal_quarter_invalid"))
    note = item.get("note")
    if note is not None and not isinstance(note, str):
        error("note", msg("events.input.note_not_text"))
    elif note and len(note) > 1000:
        error("note", msg("events.input.note_too_long", max=1000))
    if any(e["index"] == index for e in errors):
        return None
    event = {"ticker": ticker.strip().upper(), "date": day, "session": session,
             "name": name.strip()}
    if year is not None:
        event.update(fiscal_year=year, fiscal_quarter=quarter)
    if note and note.strip():
        event["note"] = note.strip()
    return event


def parse_events(text, kind):
    """Return the normalized events or raise EventInputError listing every problem."""
    if kind not in KINDS:
        raise UserError("events.input.kind_invalid")
    if not isinstance(text, str) or not text.strip():
        raise EventInputError([{"index": None, "field": None, "message": msg("events.input.empty")}])
    if len(text.encode()) > MAX_TEXT:
        raise EventInputError([{"index": None, "field": None,
                                "message": msg("events.input.too_large", kb=MAX_TEXT // 1024)}])
    payload = _json(text)
    if isinstance(payload, list):
        payload = {"events": payload}
    if not isinstance(payload, dict):
        raise EventInputError([{"index": None, "field": None,
                                "message": msg("events.input.not_object")}])
    if "schema_version" in payload:
        raise EventInputError([{"index": None, "field": "schema_version",
            "message": msg("events.input.legacy_format")}])
    extra = [key for key in payload if key != "events"]
    errors = [{"index": None, "field": key, "message": msg("events.input.unknown_top_field")}
              for key in extra]
    items = payload.get("events")
    if not isinstance(items, list) or not items:
        errors.append({"index": None, "field": "events", "message": msg("events.input.events_empty")})
        raise EventInputError(errors)
    if len(items) > MAX_EVENTS:
        errors.append({"index": None, "field": "events",
                       "message": msg("events.input.too_many", max=MAX_EVENTS, count=len(items))})
        raise EventInputError(errors)
    events = [_event(index, item, kind, errors) for index, item in enumerate(items, 1)]
    seen, periods = {}, {}
    for index, event in enumerate(events, 1):
        if event is None:
            continue
        key = (event["ticker"], event["date"], event["name"].casefold())
        if key in seen:
            errors.append({"index": index, "field": None,
                           "message": msg("events.input.duplicate_event", other=seen[key])})
        seen.setdefault(key, index)
        if kind == "earnings":
            period = (event["ticker"], event["fiscal_year"], event["fiscal_quarter"])
            if period in periods:
                errors.append({"index": index, "field": "fiscal_quarter", "message":
                    msg("events.input.duplicate_quarter", ticker=period[0], fiscal_year=period[1],
                        fiscal_quarter=period[2], other=periods[period])})
            periods.setdefault(period, index)
    if errors:
        raise EventInputError(errors)
    return sorted(events, key=lambda e: (e["date"], e["ticker"]))
