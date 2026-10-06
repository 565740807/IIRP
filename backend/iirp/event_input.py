"""The short event JSON pasted back from an external AI (S3).

    {"events": [{"ticker": "AAPL", "date": "2025-10-30", "session": "after_close",
                 "name": "FY2025 Q4 earnings", "fiscal_year": 2025, "fiscal_quarter": 4,
                 "note": "optional"}]}

Validation names the item (1-based), the field and the reason, so a user can
fix the text by hand or ask the AI again. Nothing is fetched or verified here.
"""

import json
import re
from datetime import date

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
        super().__init__("；".join(_describe(error) for error in errors[:5]))


def _describe(error):
    where = f"第 {error['index']} 条" if error.get("index") else "整体"
    field = f" {error['field']}" if error.get("field") else ""
    return f"{where}{field}：{error['message']}"


def _json(text):
    text = text.strip()
    fenced = re.fullmatch(r"```[a-zA-Z]*\s*\n([\s\S]*?)\n?```", text)
    if fenced:
        text = fenced.group(1).strip()

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"字段 {key} 重复出现")
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f"JSON 不接受 {value}")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except json.JSONDecodeError as exc:
        raise EventInputError([{"index": None, "field": None,
            "message": f"不是有效的 JSON（第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}）"}]) from None
    except ValueError as exc:
        raise EventInputError([{"index": None, "field": None, "message": str(exc)}]) from None


def _int(value):
    return value if type(value) is int else None


def _event(index, item, kind, errors):
    def error(field, message):
        errors.append({"index": index, "field": field, "message": message})

    if not isinstance(item, dict):
        error(None, "每条事件须是一个 JSON 对象")
        return None
    for key in item:
        if key not in FIELDS:
            error(key, f"不认识的字段；只接受 {', '.join(FIELDS)}")
    ticker = item.get("ticker")
    if not isinstance(ticker, str) or not ticker.strip():
        error("ticker", "缺少股票代码，例如 \"AAPL\"")
    elif not TICKER.fullmatch(ticker.strip().upper()):
        error("ticker", f"股票代码格式不对：{ticker!r}")
    day = item.get("date")
    parsed = None
    if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        error("date", f"日期须为 YYYY-MM-DD，收到 {day!r}")
    else:
        try:
            parsed = date.fromisoformat(day)
        except ValueError:
            error("date", f"不是真实的日历日期：{day}")
        if parsed and parsed.year < 1990:
            error("date", "日期早于 1990 年")
    session = item.get("session")
    if session not in SESSIONS:
        error("session", f"须为 {' / '.join(SESSIONS)} 之一（不确定填 unknown），收到 {session!r}")
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        error("name", "缺少事件名称")
    elif len(name) > 200:
        error("name", "名称不能超过 200 个字符")
    year, quarter = item.get("fiscal_year"), item.get("fiscal_quarter")
    if kind == "earnings" or year is not None or quarter is not None:
        if _int(year) is None or not 1990 <= year <= 2100:
            error("fiscal_year", "财报须给出财年（整数，例如 2025）" if kind == "earnings"
                  else "财年须为整数，例如 2025")
        if _int(quarter) is None or not 1 <= quarter <= 4:
            error("fiscal_quarter", "财报须给出财季（整数 1–4）" if kind == "earnings"
                  else "财季须为整数 1–4")
    note = item.get("note")
    if note is not None and not isinstance(note, str):
        error("note", "备注须为文字")
    elif note and len(note) > 1000:
        error("note", "备注不能超过 1000 个字符")
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
        raise ValueError("事件类型须为 earnings 或 custom")
    if not isinstance(text, str) or not text.strip():
        raise EventInputError([{"index": None, "field": None, "message": "请粘贴 JSON"}])
    if len(text.encode()) > MAX_TEXT:
        raise EventInputError([{"index": None, "field": None,
                                "message": f"内容超过 {MAX_TEXT // 1024} KB"}])
    payload = _json(text)
    if isinstance(payload, list):
        payload = {"events": payload}
    if not isinstance(payload, dict):
        raise EventInputError([{"index": None, "field": None,
                                "message": "须为 {\"events\": [...]} 形式的 JSON 对象"}])
    if "schema_version" in payload:
        raise EventInputError([{"index": None, "field": "schema_version",
            "message": "旧的导入格式已不再支持；请用页面上的提示词重新生成简短 JSON"}])
    extra = [key for key in payload if key != "events"]
    errors = [{"index": None, "field": key, "message": "不认识的顶层字段；只需要 events"}
              for key in extra]
    items = payload.get("events")
    if not isinstance(items, list) or not items:
        errors.append({"index": None, "field": "events", "message": "events 须为非空数组"})
        raise EventInputError(errors)
    if len(items) > MAX_EVENTS:
        errors.append({"index": None, "field": "events",
                       "message": f"最多 {MAX_EVENTS} 条，收到 {len(items)} 条"})
        raise EventInputError(errors)
    events = [_event(index, item, kind, errors) for index, item in enumerate(items, 1)]
    seen, periods = {}, {}
    for index, event in enumerate(events, 1):
        if event is None:
            continue
        key = (event["ticker"], event["date"], event["name"].casefold())
        if key in seen:
            errors.append({"index": index, "field": None,
                           "message": f"与第 {seen[key]} 条重复（股票、日期和名称相同）"})
        seen.setdefault(key, index)
        if kind == "earnings":
            period = (event["ticker"], event["fiscal_year"], event["fiscal_quarter"])
            if period in periods:
                errors.append({"index": index, "field": "fiscal_quarter", "message":
                    f"{period[0]} FY{period[1]} Q{period[2]} 与第 {periods[period]} 条重复；每个财季只保留一次首次公布"})
            periods.setdefault(period, index)
    if errors:
        raise EventInputError(errors)
    return sorted(events, key=lambda e: (e["date"], e["ticker"]))
