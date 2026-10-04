"""Strict, evidence-carrying AI import documents; approval is always server owned.

No source is fetched by this module. A supported claim is a candidate, never an
independently verified fact. Keep this schema as the prompt/OpenAPI authority.
"""

import json
import re
from datetime import date, datetime, timezone
from typing import Annotated, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from iirp.import_limits import validate_event_text


def _date(value):
    if type(value) is date:
        return value
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("日期须为 YYYY-MM-DD，未知日期请用 null")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("日期不是有效日历日期") from None


def _url(value):
    if not isinstance(value, str) or "\\" in value or any(ord(char) <= 32 for char in value):
        raise ValueError("来源链接须为无空白的 HTTP/HTTPS 地址")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError
        parsed.port
    except ValueError:
        raise ValueError("来源链接须为有效 HTTP/HTTPS 地址且不能含登录信息") from None
    return value


def _timezone(value):
    if value is None:
        return None
    if not isinstance(value, str) or ("/" not in value and value != "UTC"):
        raise ValueError("时区须使用 IANA 名称，如 America/New_York")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("未知 IANA 时区") from None
    return value


ISODate = Annotated[date, BeforeValidator(_date)]
SourceURL = Annotated[str, BeforeValidator(_url)]
Nonempty = Annotated[str, Field(min_length=1, pattern=r".*\S.*")]
Year = Annotated[int, Field(ge=1, le=9998)]
Quarter = Annotated[int, Field(ge=1, le=4)]
EventType = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
TimeZone = Annotated[str | None, BeforeValidator(_timezone)]
CustomSupport = Literal["event_date", "event_time", "timezone", "event_status", "event_type"]
EarningsSupport = Literal[
    "event_date",
    "event_time",
    "timezone",
    "release_session",
    "event_status",
    "fiscal_year",
    "fiscal_quarter",
    "period_end",
    "period_start",
    "period_kind",
]


class StrictEventModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EventCompany(StrictEventModel):
    name: Nonempty
    ticker: Annotated[str, Field(pattern=r"^[A-Z0-9^][A-Z0-9.^=\-]{0,19}$")] | None
    exchange_mic: Annotated[str, Field(pattern=r"^[A-Z0-9]{4}$")] | None


class EventSource(StrictEventModel):
    url: SourceURL
    title: Nonempty
    publisher: Nonempty
    published_date: ISODate | None
    source_kind: Literal["primary", "secondary", "unknown"]
    supports: list[CustomSupport]
    evidence_note: Nonempty

    @model_validator(mode="after")
    def unique_supports(self):
        if len(set(self.supports)) != len(self.supports):
            raise ValueError("来源 supports 不能重复")
        return self


class EarningsSource(EventSource):
    supports: list[EarningsSupport]


class EventFields(StrictEventModel):
    client_event_id: Nonempty
    event_name: Nonempty
    event_date: ISODate | None
    event_time: Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")] | None
    timezone: TimeZone
    time_precision: Literal["minute", "date", "unknown"]
    time_basis: Literal["official_schedule", "reported_actual", "unknown"]
    event_status: Literal["occurred", "scheduled", "cancelled", "unknown"]
    date_status: Literal["supported", "conflicting", "unverified"]
    notes: str | None

    def check_evidence(self):
        supported = {field for source in self.sources for field in source.supports}
        if self.date_status == "conflicting" and self.event_date is not None:
            raise ValueError("日期冲突时 event_date 须为 null，候选日期保留在来源说明")
        if self.event_date is None:
            if (
                self.event_time is not None
                or self.time_precision != "unknown"
                or self.time_basis != "unknown"
                or self.date_status == "supported"
            ):
                raise ValueError("未知日期不能附带时刻、日期精度或 supported 状态")
        elif self.event_time is None:
            if self.time_precision != "date" or self.time_basis != "unknown":
                raise ValueError("只有日期时须用 date 精度、unknown 时间依据")
        else:
            if (
                self.timezone is None
                or self.time_precision != "minute"
                or self.time_basis == "unknown"
                or not {"event_time", "timezone"}.issubset(supported)
            ):
                raise ValueError("分钟时刻需要日期、IANA 时区、时间依据和对应来源")
            wall = datetime.fromisoformat(f"{self.event_date}T{self.event_time}")
            zone = ZoneInfo(self.timezone)
            valid = []
            for fold in (0, 1):
                candidate = wall.replace(tzinfo=zone, fold=fold)
                if candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == wall:
                    valid.append(candidate.utcoffset())
            if not valid or len(set(valid)) != 1:
                raise ValueError("时刻落在夏令时不存在或重复的分钟，须核对后改用仅日期")
        if self.date_status == "supported" and "event_date" not in supported:
            raise ValueError("supported 日期须有明确支持 event_date 的来源")
        if self.event_status == "occurred" and "event_status" not in supported:
            raise ValueError("已发生事件须有支持实际发生的来源")
        if self.time_basis == "reported_actual" and self.event_status != "occurred":
            raise ValueError("实际发布时刻只能用于已发生事件")
        if self.event_date is not None and self.event_year != self.event_date.year:
            raise ValueError("event_year 须与事件当地日期的自然年一致")
        return self


class CustomEvent(EventFields):
    event_type: EventType
    event_year: Year
    sources: list[EventSource]

    @model_validator(mode="after")
    def consistent(self):
        return self.check_evidence()


class EarningsEvent(EventFields):
    event_type: Literal["earnings_release"]
    event_year: Year | None
    fiscal_year: Year | None
    fiscal_quarter: Quarter | None
    period_end: ISODate | None
    period_start: ISODate | None = None
    period_kind: Literal["regular", "transition", "unknown"]
    release_session: Literal["before_open", "during_session", "after_close", "unknown"]
    sources: list[EarningsSource]

    @model_validator(mode="after")
    def consistent(self):
        self.check_evidence()
        support = {field for source in self.sources for field in source.supports}
        if self.event_date is None and self.event_year is not None:
            raise ValueError("发布日期未知时 event_year 须为 null")
        if self.period_end and self.event_date and self.period_end > self.event_date:
            raise ValueError("报告期末不能晚于业绩发布日期")
        if self.period_start and (not self.period_end or self.period_start > self.period_end or "period_start" not in support):
            raise ValueError("财期起点须有 period_start 来源支持、真实期末且不晚于期末")
        if self.release_session != "unknown":
            if self.event_date is None or "release_session" not in support:
                raise ValueError("发布时段需要日期和支持 release_session 的来源")
        return self


class CustomEventScope(StrictEventModel):
    year_start: Year
    year_end: Year
    event_types: list[EventType] = Field(min_length=1)
    include_keywords: list[Nonempty]
    exclude_keywords: list[Nonempty]
    anchor_basis: Literal["event_start"]
    include_scheduled: bool

    @model_validator(mode="after")
    def consistent(self):
        if self.year_start > self.year_end:
            raise ValueError("开始年份不能晚于结束年份")
        if len(set(self.event_types)) != len(self.event_types):
            raise ValueError("事件类型不能重复")
        return self


class EarningsEventScope(StrictEventModel):
    fiscal_year_start: Year
    fiscal_year_end: Year
    fiscal_quarters: list[Quarter] = Field(min_length=1)
    anchor_basis: Literal["earnings_release"]
    include_scheduled: bool

    @model_validator(mode="after")
    def consistent(self):
        if self.fiscal_year_start > self.fiscal_year_end:
            raise ValueError("开始财年不能晚于结束财年")
        if len(set(self.fiscal_quarters)) != len(self.fiscal_quarters):
            raise ValueError("季度不能重复")
        return self


class CoverageFields(StrictEventModel):
    search_status: Literal["searched", "partial", "not_searched"]
    result_status: Literal["events_found", "not_found", "confirmed_none", "unresolved"]
    event_ids: list[Nonempty]
    source_urls: list[SourceURL]
    notes: Nonempty

    @model_validator(mode="after")
    def consistent(self):
        if len(set(self.event_ids)) != len(self.event_ids):
            raise ValueError("同一覆盖项不能重复引用事件")
        if len(set(self.source_urls)) != len(self.source_urls):
            raise ValueError("同一覆盖项不能重复来源链接")
        if self.result_status == "events_found" and not self.event_ids:
            raise ValueError("events_found 须列出找到的事件 ID")
        if self.result_status in {"not_found", "confirmed_none"} and self.event_ids:
            raise ValueError("未找到/明确未举办不能同时引用事件")
        if self.result_status == "confirmed_none" and (
            self.search_status != "searched" or not self.source_urls
        ):
            raise ValueError("明确未举办须已查询并提供支持来源")
        if self.search_status == "not_searched" and (
            self.result_status != "unresolved" or self.event_ids
        ):
            raise ValueError("未查询的覆盖项只能为 unresolved 且不能列入事件")
        return self


class CustomEventCoverage(CoverageFields):
    year: Year
    event_type: EventType


class EarningsEventCoverage(CoverageFields):
    fiscal_year: Year
    fiscal_quarter: Quarter


def _check_document(document, earnings=False):
    if document.research_as_of > datetime.now(timezone.utc).date():
        raise ValueError("查询截止日期不能在未来")
    if len({event.client_event_id for event in document.events}) != len(document.events):
        raise ValueError("client_event_id 必须在文件内唯一")
    scope = document.scope
    first = scope.fiscal_year_start if earnings else scope.year_start
    last = scope.fiscal_year_end if earnings else scope.year_end
    groups = scope.fiscal_quarters if earnings else scope.event_types
    expected = {(year, group) for year in range(first, last + 1) for group in groups}
    actual = {}
    event_keys = {}
    for event in document.events:
        if event.event_date and event.event_date > document.research_as_of:
            if event.event_status == "occurred":
                raise ValueError("查询截止日之后的事件不能标为已发生")
            if not scope.include_scheduled:
                raise ValueError("当前范围不包含未来计划")
        if event.event_status == "scheduled" and not scope.include_scheduled:
            raise ValueError("当前范围不包含计划事件")
        key = (
            (event.fiscal_year, event.fiscal_quarter)
            if earnings
            else (event.event_year, event.event_type)
        )
        if earnings and None in key:
            if (key[0] is not None and not first <= key[0] <= last) or (
                key[1] is not None and key[1] not in groups
            ):
                raise ValueError("已知财期部分不属于请求范围")
            if not document.warnings:
                raise ValueError("财期归属不明须在 warnings 说明")
        elif key not in expected:
            raise ValueError("事件年份或类别不属于请求范围")
        event_keys[event.client_event_id] = key
    referenced = set()
    for coverage in document.coverage:
        key = (
            (coverage.fiscal_year, coverage.fiscal_quarter)
            if earnings
            else (coverage.year, coverage.event_type)
        )
        if key in actual:
            raise ValueError("年份与类别的覆盖项不能重复")
        actual[key] = coverage
        for event_id in coverage.event_ids:
            if event_id not in event_keys:
                raise ValueError(f"覆盖引用不存在的事件：{event_id}")
            if event_keys[event_id] != key:
                raise ValueError("覆盖引用的事件须属于同一年份与类别")
            referenced.add(event_id)
    if set(actual) != expected:
        raise ValueError("coverage 须完整列出每个请求年份与类别，不能漏项或多项")
    for event_id, key in event_keys.items():
        if None not in key and event_id not in referenced:
            raise ValueError(f"覆盖项遗漏已找到的事件：{event_id}")
        if None in key and not any(row.result_status == "unresolved" for row in actual.values()):
            raise ValueError("归属不明的财报须有 unresolved 覆盖项，不能声称全部已解决")
    return document


class CustomEventsImport(StrictEventModel):
    schema_version: Literal["iirp.custom-events.v1"]
    research_as_of: ISODate
    company: EventCompany
    scope: CustomEventScope
    events: list[CustomEvent]
    coverage: list[CustomEventCoverage]
    warnings: list[Nonempty]

    @model_validator(mode="after")
    def consistent(self):
        return _check_document(self)


class EarningsEventsImport(StrictEventModel):
    schema_version: Literal["iirp.earnings-events.v1"]
    research_as_of: ISODate
    company: EventCompany
    scope: EarningsEventScope
    events: list[EarningsEvent]
    coverage: list[EarningsEventCoverage]
    warnings: list[Nonempty]

    @model_validator(mode="after")
    def consistent(self):
        return _check_document(self, earnings=True)


def parse_event_import(text: str) -> CustomEventsImport | EarningsEventsImport:
    """Parse one document, retaining JSON line/column errors and rejecting duplicate keys."""
    if not isinstance(text, str):
        raise ValueError("请粘贴 JSON 文本")
    validate_event_text(text)
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n([\s\S]*)\n```", text)
    if fenced:
        text = fenced.group(1).strip()

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"JSON 字段重复：{key}")
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f"JSON 不接受 {value}")

    payload = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(payload, dict):
        raise ValueError("导入内容须为一个 JSON 对象")
    version = payload.get("schema_version")
    if not isinstance(version, str):
        raise ValueError("事件格式版本须为已支持的字符串")
    if version in {"iirp.custom.simple.v1", "iirp.earnings.simple.v1"}:
        from iirp.event_simple import normalize_simple
        return normalize_simple(payload)
    model = {
        "iirp.custom-events.v1": CustomEventsImport,
        "iirp.earnings-events.v1": EarningsEventsImport,
    }.get(version)
    if model is None:
        raise ValueError("不支持的事件格式版本")
    return model.model_validate(payload)


def import_warnings(document: CustomEventsImport | EarningsEventsImport) -> list[str]:
    """Review hints only; no fact is approved by validation or duplicate heuristics."""
    result = ["AI 资料，待核对；格式检查通过不等于事实已验证", *document.warnings]
    seen = set()
    periods = set()
    for event in document.events:
        if any(not source.supports for source in event.sources):
            result.append(f"{event.event_name}：已保留来源链接，但未说明支持哪些事实，请补充支持关系后再核对")
        key = (event.event_type, event.event_name.casefold().strip(), event.event_date)
        if key in seen:
            result.append(f"疑似重复事件：{event.event_name}；请核对是否同一活动")
        seen.add(key)
        if event.event_date and event.event_time is None:
            result.append(f"{event.event_name}：只有日期，按交易所日期近似对齐，未推断盘前/盘后")
        if event.time_basis == "official_schedule":
            result.append(f"{event.event_name}：时刻来自官方日程，不代表已核对实际开始分钟")
        if isinstance(document, EarningsEventsImport) and event.period_kind == "regular" and not any(
            "period_kind" in source.supports for source in event.sources
        ):
            result.append(f"{event.event_name}：标为常规财期但来源未支持 period_kind；日期价格可观察，不能核对为常规财期汇总样本")
        if (
            isinstance(document, EarningsEventsImport)
            and event.fiscal_year
            and event.fiscal_quarter
        ):
            period = (event.fiscal_year, event.fiscal_quarter)
            if period in periods:
                result.append(
                    f"同一财期存在多个主发布候选：FY{period[0]} Q{period[1]}；请核对是否修订或重复"
                )
            periods.add(period)
        if event.date_status != "supported":
            result.append(f"{event.event_name}：日期待核对，不进入默认汇总")
    if any(row.search_status != "searched" for row in document.coverage):
        result.append("存在部分查询或未查询年份；不能标为完整历史覆盖")
    result.append("searched 仅为导入资料报告的查询状态，不证明该年没有遗漏")
    return list(dict.fromkeys(result))
