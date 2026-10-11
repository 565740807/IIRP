"""The event JSON pasted back from an external AI.

    {"events": [{"ticker": "AAPL", "date": "2025-10-30", "session": "after_close",
                 "name": "FY2025 Q4 earnings", "fiscal_year": 2025, "fiscal_quarter": 4,
                 "uncertain": false, "note": "optional",
                 "earnings": {"eps_actual": 1.85, "eps_estimate": 1.77,
                              "revenue_actual": 102466, "revenue_estimate": 102227,
                              "guidance": "maintained"},
                 "context": [{"date": "2025-10-29", "category": "macro",
                              "title": "One sentence", "summary": "At most 300 characters",
                              "source": "Publisher", "url": "https://...", "major": true}]}]}

Only ticker, date, session and name (and the fiscal period of earnings) are
required; a list without the optional fields is still accepted as it always
was. Errors name the item (1-based), the field and the reason, so a user can
fix the text by hand or ask the AI again. Warnings point at context outside
the chosen window, too many context items, missing sources and entries the
AI marked uncertain; they do not block saving. Nothing is fetched or
verified here: context and estimates are the AI's own, unverified.
"""

import json
import re
from datetime import date, timedelta
from decimal import Decimal

from iirp.messages import UserError, msg

KINDS = ("earnings", "custom")
SESSIONS = ("before_open", "during", "after_close", "unknown")
FIELDS = ("ticker", "date", "session", "name", "fiscal_year", "fiscal_quarter", "uncertain", "note",
          "earnings", "context")
CATEGORIES = ("company", "earnings", "industry", "market", "macro", "policy", "geopolitical", "other")
GUIDANCE = ("raised", "maintained", "lowered", "none")
EARNINGS_FIELDS = ("eps_actual", "eps_estimate", "revenue_actual", "revenue_estimate", "guidance")
CONTEXT_FIELDS = ("date", "category", "title", "summary", "source", "url", "major")
# Asked for in the prompt; more is a warning. The hard limits keep a set small.
CONTEXT_ITEMS, CONTEXT_ITEMS_LIMIT = 8, 30
SUMMARY_CHARS, SUMMARY_LIMIT = 300, 2000
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


def _day(value):
    """(date, None) or (None, message) for a YYYY-MM-DD string."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None, msg("events.input.date_format", value=repr(value))
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None, msg("events.input.date_invalid", value=value)
    if parsed.year < 1990:
        return None, msg("events.input.date_too_early")
    return parsed, None


def _text(value, error, field, limit):
    """Stripped text, None when absent, or an error for non-text or over-long values."""
    if value is None:
        return None
    if not isinstance(value, str):
        error(field, msg("events.input.text_required"))
        return None
    if len(value) > limit:
        error(field, msg("events.input.text_too_long", max=limit))
        return None
    return value.strip() or None


def _flag(value, error, field):
    if value is not None and type(value) is not bool:
        error(field, msg("events.input.boolean_required", value=repr(value)))
    return value is True


def _earnings(value, kind, error):
    if value is None:
        return None
    if kind != "earnings":
        error("earnings", msg("events.input.earnings_custom"))
        return None
    if not isinstance(value, dict):
        error("earnings", msg("events.input.earnings_not_object"))
        return None
    output = {}
    for key, item in value.items():
        field = f"earnings.{key}"
        if key not in EARNINGS_FIELDS:
            error(field, msg("events.input.unknown_field", fields=", ".join(EARNINGS_FIELDS)))
        elif item is None:
            continue
        elif key == "guidance":
            if item not in GUIDANCE:
                error(field, msg("events.input.guidance_invalid", values=" / ".join(GUIDANCE), value=repr(item)))
            else:
                output[key] = item
        elif type(item) not in (int, float) or not Decimal(str(item)).is_finite():
            error(field, msg("events.input.number_required", value=repr(item)))
        elif key.startswith("revenue") and item < 0:
            error(field, msg("events.input.revenue_negative"))
        else:
            output[key] = item
    return output or None


def _context(value, event_day, error, warn, window):
    if value is None:
        return None
    if not isinstance(value, list):
        error("context", msg("events.input.context_not_list"))
        return None
    if len(value) > CONTEXT_ITEMS_LIMIT:
        error("context", msg("events.input.context_limit", max=CONTEXT_ITEMS_LIMIT, count=len(value)))
        return None
    if len(value) > CONTEXT_ITEMS:
        warn("context", msg("events.input.context_too_many", max=CONTEXT_ITEMS, count=len(value)))
    items = []
    for position, item in enumerate(value, 1):
        prefix = f"context[{position}]"
        if not isinstance(item, dict):
            error(prefix, msg("events.input.context_item_not_object"))
            continue
        problems = []

        def fail(field, message, problems=problems):
            problems.append(field)
            error(field, message)

        for key in item:
            if key not in CONTEXT_FIELDS:
                fail(f"{prefix}.{key}", msg("events.input.unknown_field", fields=", ".join(CONTEXT_FIELDS)))
        day, problem = _day(item.get("date"))
        if problem:
            fail(f"{prefix}.date", problem)
        category = item.get("category")
        if category not in CATEGORIES:
            fail(f"{prefix}.category", msg("events.input.context_category", values=" / ".join(CATEGORIES),
                                           value=repr(category)))
        title = _text(item.get("title"), fail, f"{prefix}.title", 200)
        if title is None and f"{prefix}.title" not in problems:
            fail(f"{prefix}.title", msg("events.input.context_title_missing"))
        summary = _text(item.get("summary"), fail, f"{prefix}.summary", SUMMARY_LIMIT)
        source = _text(item.get("source"), fail, f"{prefix}.source", 200)
        url = _text(item.get("url"), fail, f"{prefix}.url", 2000)
        if url and not re.fullmatch(r"https?://[^\s/$.?#][^\s]*", url, re.IGNORECASE):
            fail(f"{prefix}.url", msg("events.input.url_invalid", value=repr(url[:80])))
            url = None
        major = _flag(item.get("major"), fail, f"{prefix}.major")
        if problems:
            continue
        if summary and len(summary) > SUMMARY_CHARS:
            warn(f"{prefix}.summary", msg("events.input.summary_long", max=SUMMARY_CHARS, count=len(summary)))
        if not source:
            warn(f"{prefix}.source", msg("events.input.context_source_missing"))
        if not url:
            warn(f"{prefix}.url", msg("events.input.context_url_missing"))
        if event_day and window and not (event_day - timedelta(days=window[0]) <= day
                                         <= event_day + timedelta(days=window[1])):
            warn(f"{prefix}.date", msg("events.input.context_outside", date=str(day),
                                       before=window[0], after=window[1]))
        entry = {"date": str(day), "category": category, "title": title}
        entry.update({key: value for key, value in (("summary", summary), ("source", source), ("url", url))
                      if value})
        if major:
            entry["major"] = True
        items.append(entry)
    return sorted(items, key=lambda entry: entry["date"]) or None


def _event(index, item, kind, errors, warnings, window):
    def error(field, message):
        errors.append({"index": index, "field": field, "message": message})

    def warn(field, message):
        warnings.append({"index": index, "field": field, "message": message})

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
    parsed, problem = _day(day)
    if problem:
        error("date", problem)
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
    uncertain = _flag(item.get("uncertain"), error, "uncertain")
    figures = _earnings(item.get("earnings"), kind, error)
    context = _context(item.get("context"), parsed, error, warn, window)
    if any(e["index"] == index for e in errors):
        return None
    if uncertain:
        warn("uncertain", msg("events.input.uncertain"))
    event = {"ticker": ticker.strip().upper(), "date": day, "session": session,
             "name": name.strip()}
    if year is not None:
        event.update(fiscal_year=year, fiscal_quarter=quarter)
    if uncertain:
        event["uncertain"] = True
    if note and note.strip():
        event["note"] = note.strip()
    if figures:
        event["earnings"] = figures
    if context:
        event["context"] = context
    return event


def parse_events(text, kind, window=None):
    """Return the normalized events or raise EventInputError listing every problem."""
    return check_events(text, kind, window)[0]


def check_events(text, kind, window=None):
    """(events, warnings), or EventInputError listing every problem.

    ``window`` is (days before, days after) the event date that context should
    fall within; without it context dates are not checked.
    """
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
    warnings = []
    events = [_event(index, item, kind, errors, warnings, window) for index, item in enumerate(items, 1)]
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
    return sorted(events, key=lambda e: (e["date"], e["ticker"])), warnings
