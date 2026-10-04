"""Small external input, normalized into the existing strict immutable document.

Defaults describe missing information only. Explicit source claims are candidates;
approval still belongs to event_service._review, never this adapter.
"""

import hashlib
import json
from typing import Literal

from pydantic import Field

from iirp.event_contracts import (
    CustomEventsImport,
    EarningsEventsImport,
    EventCompany,
    EventType,
    ISODate,
    Nonempty,
    Quarter,
    SourceURL,
    StrictEventModel,
    TimeZone,
    Year,
)

CLAIMS = {
    "日期": ["event_date"],
    "已发生": ["event_status"],
    "时间": ["event_time"],
    "时区": ["timezone"],
    "事件类型": ["event_type"],
    "财年": ["fiscal_year"],
    "财季": ["fiscal_quarter"],
    "财年财季": ["fiscal_year", "fiscal_quarter"],
    "财期起点": ["period_start"],
    "财期终点": ["period_end"],
    "财期起止": ["period_start", "period_end"],
    "财期类型": ["period_kind"],
    "公告时段": ["release_session"],
}
Claim = Literal[
    "日期",
    "已发生",
    "时间",
    "时区",
    "事件类型",
    "财年",
    "财季",
    "财年财季",
    "财期起点",
    "财期终点",
    "财期起止",
    "财期类型",
    "公告时段",
]


class SimpleCompany(EventCompany):
    ticker: EventCompany.model_fields["ticker"].annotation = None
    exchange_mic: EventCompany.model_fields["exchange_mic"].annotation = None


class SimpleSource(StrictEventModel):
    url: SourceURL
    note: Nonempty
    supports: list[Claim] = Field(default_factory=list)
    title: Nonempty = "导入来源（未提供标题）"
    publisher: Nonempty = "未提供发布者"
    published_date: ISODate | None = None
    source_kind: Literal["primary", "secondary", "unknown"] = "unknown"

    def normalized(self):
        value = self.model_dump(mode="json", exclude={"note", "supports"})
        return {
            **value,
            "evidence_note": self.note,
            "supports": list(dict.fromkeys(f for c in self.supports for f in CLAIMS[c])),
        }


CustomClaim = Literal["日期", "已发生", "时间", "时区", "事件类型"]
EarningsClaim = Literal[
    "日期", "已发生", "时间", "时区", "财年", "财季", "财年财季",
    "财期起点", "财期终点", "财期起止", "财期类型", "公告时段",
]


class SimpleCustomSource(SimpleSource):
    supports: list[CustomClaim] = Field(default_factory=list)


class SimpleEarningsSource(SimpleSource):
    supports: list[EarningsClaim] = Field(default_factory=list)


class SimpleEvent(StrictEventModel):
    name: Nonempty | None = None
    date: ISODate | None
    status: Literal["已发生", "计划", "取消", "未知"] = "未知"
    date_conflict: bool = False
    time_conflict: bool = False
    time: str | None = None
    timezone: TimeZone = None
    time_basis: Literal["实际发生", "官方日程", "未知"] = "未知"
    sources: list[SimpleSource] = Field(default_factory=list)
    note: str | None = None

    def normalized(self, index):
        if self.time_conflict and (
            self.time is not None
            or self.time_basis != "未知"
            or getattr(self, "release_session", "未知") != "未知"
        ):
            raise ValueError(
                "时间冲突须用 time=null、time_basis=未知、release_session=未知，候选时间保留在来源说明"
            )
        sources = [source.normalized() for source in self.sources]
        claims = {f for source in sources for f in source["supports"]}
        return {
            "client_event_id": f"import-{index + 1}",
            "event_name": self.name,
            "event_date": self.date.isoformat() if self.date else None,
            "event_time": self.time,
            "timezone": self.timezone,
            "time_precision": "minute" if self.time else "date" if self.date else "unknown",
            "time_basis": {
                "实际发生": "reported_actual",
                "官方日程": "official_schedule",
                "未知": "unknown",
            }[self.time_basis],
            "event_status": {
                "已发生": "occurred",
                "计划": "scheduled",
                "取消": "cancelled",
                "未知": "unknown",
            }[self.status],
            "date_status": "conflicting"
            if self.date_conflict
            else "supported"
            if self.date and "event_date" in claims
            else "unverified",
            "sources": sources,
            "notes": f"实际时刻/时段有冲突，按日期观察，候选见来源。{self.note or ''}"
            if self.time_conflict
            else self.note,
        }


class SimpleCustomEvent(SimpleEvent):
    sources: list[SimpleCustomSource] = Field(default_factory=list)
    name: Nonempty
    event_type: EventType
    year: Year | None = None

    def normalized(self, index):
        if self.date is None and self.year is None:
            raise ValueError("日期未知的自定义事件须提供 year 所属目标年份")
        return {
            **super().normalized(index),
            "event_type": self.event_type,
            "event_year": self.year if self.year is not None else self.date.year,
        }


class SimpleEarningsEvent(SimpleEvent):
    sources: list[SimpleEarningsSource] = Field(default_factory=list)
    fiscal_year: Year | None
    fiscal_quarter: Quarter | None
    period_start: ISODate | None = None
    period_end: ISODate | None = None
    period_kind: Literal["常规", "过渡", "未知"] = "未知"
    release_session: Literal["盘前", "盘中", "盘后", "未知"] = "未知"

    def normalized(self, index):
        value = super().normalized(index)
        return {
            **value,
            "event_name": self.name
            or f"FY{self.fiscal_year or '待核对'} Q{self.fiscal_quarter or '待核对'} 业绩发布",
            "event_type": "earnings_release",
            "event_year": self.date.year if self.date else None,
            "fiscal_year": self.fiscal_year,
            "fiscal_quarter": self.fiscal_quarter,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "period_kind": {"常规": "regular", "过渡": "transition", "未知": "unknown"}[
                self.period_kind
            ],
            "release_session": {
                "盘前": "before_open",
                "盘中": "during_session",
                "盘后": "after_close",
                "未知": "unknown",
            }[self.release_session],
        }


class SimpleCustomScope(StrictEventModel):
    year_start: Year
    year_end: Year
    event_types: list[EventType] = Field(min_length=1)
    include_keywords: list[Nonempty] = Field(default_factory=list)
    exclude_keywords: list[Nonempty] = Field(default_factory=list)
    include_scheduled: bool = False


class SimpleEarningsScope(StrictEventModel):
    fiscal_year_start: Year
    fiscal_year_end: Year
    fiscal_quarters: list[Quarter] = Field(default_factory=lambda: [1, 2, 3, 4], min_length=1)
    include_scheduled: bool = False


class GapEvidence(StrictEventModel):
    url: SourceURL
    note: Nonempty


class SimpleGap(StrictEventModel):
    status: Literal["未查询", "未找到", "冲突", "部分查询", "明确未举办"]
    note: Nonempty
    sources: list[GapEvidence] = Field(default_factory=list)


class SimpleCustomGap(SimpleGap):
    year: Year
    event_type: EventType


class SimpleEarningsGap(SimpleGap):
    fiscal_year: Year
    fiscal_quarter: Quarter


class SimpleCustomImport(StrictEventModel):
    schema_version: Literal["iirp.custom.simple.v1"]
    research_as_of: ISODate
    company: SimpleCompany
    scope: SimpleCustomScope
    events: list[SimpleCustomEvent]
    gaps: list[SimpleCustomGap] = Field(default_factory=list)
    warnings: list[Nonempty] = Field(default_factory=list)


class SimpleEarningsImport(StrictEventModel):
    schema_version: Literal["iirp.earnings.simple.v1"]
    research_as_of: ISODate
    company: SimpleCompany
    scope: SimpleEarningsScope
    events: list[SimpleEarningsEvent]
    gaps: list[SimpleEarningsGap] = Field(default_factory=list)
    warnings: list[Nonempty] = Field(default_factory=list)


def normalize_simple(payload):
    fiscal = payload["schema_version"] == "iirp.earnings.simple.v1"
    simple = (SimpleEarningsImport if fiscal else SimpleCustomImport).model_validate(payload)
    scope = simple.scope.model_dump(mode="json")
    scope["anchor_basis"] = "earnings_release" if fiscal else "event_start"
    first, last = (
        (scope["fiscal_year_start"], scope["fiscal_year_end"])
        if fiscal
        else (scope["year_start"], scope["year_end"])
    )
    groups = scope["fiscal_quarters"] if fiscal else scope["event_types"]
    # Validate scope before expanding; never coerce booleans, dates, or fiscal facts.
    from iirp.event_contracts import CustomEventScope, EarningsEventScope

    (EarningsEventScope if fiscal else CustomEventScope).model_validate(scope)
    events = [event.normalized(index) for index, event in enumerate(simple.events)]
    duplicates = {}
    for event in events:
        identity = [
            event.get(k)
            for k in ("event_type", "event_name", "event_date", "fiscal_year", "fiscal_quarter")
        ]
        key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:24]
        duplicates[key] = duplicates.get(key, 0) + 1
        event["client_event_id"] = f"simple-{key}-{duplicates[key]}"
    keys = ("fiscal_year", "fiscal_quarter") if fiscal else ("year", "event_type")
    event_keys = ("fiscal_year", "fiscal_quarter") if fiscal else ("event_year", "event_type")
    gaps = {}
    for gap in simple.gaps:
        key = tuple(getattr(gap, field) for field in keys)
        if key in gaps or not first <= key[0] <= last or key[1] not in groups:
            raise ValueError("未找到/未查询说明的年份与类别重复或不在请求范围")
        gaps[key] = gap
    coverage = []
    for year in range(first, last + 1):
        for group in groups:
            found = [e for e in events if tuple(e[f] for f in event_keys) == (year, group)]
            gap = gaps.get((year, group))
            if found and gap and gap.status in {"未查询", "未找到", "明确未举办"}:
                raise ValueError("已有事件候选的财期/类别不能同时声明未查询、未找到或未举办")
            if (
                found
                and gap
                and gap.status == "冲突"
                and any(e["date_status"] != "conflicting" for e in found)
            ):
                raise ValueError(
                    "冲突组的候选日期须用 date=null、date_conflict=true，候选日期保留在来源说明"
                )
            search, result = (
                ("partial", "events_found") if found else ("not_searched", "unresolved")
            )
            if gap:
                search = (
                    "searched"
                    if gap.status in {"未找到", "明确未举办"}
                    else "not_searched"
                    if gap.status == "未查询"
                    else "partial"
                )
                result = (
                    "not_found"
                    if gap.status == "未找到"
                    else "confirmed_none"
                    if gap.status == "明确未举办"
                    else result
                )
            coverage.append(
                {
                    keys[0]: year,
                    keys[1]: group,
                    "search_status": search,
                    "result_status": result,
                    "event_ids": [e["client_event_id"] for e in found],
                    "source_urls": list(
                        dict.fromkeys(
                            [s["url"] for e in found for s in e["sources"]]
                            + [s.url for s in gap.sources]
                            if gap
                            else [s["url"] for e in found for s in e["sources"]]
                        )
                    ),
                    "notes": (
                        gap.status
                        + "："
                        + gap.note
                        + " "
                        + " ".join(f"{s.url}：{s.note}" for s in gap.sources)
                    )
                    if gap
                    else "已提供候选，未声明完整检索。"
                    if found
                    else "未提供该年份/类别的查询结果；不能视为没有事件。",
                }
            )
    model = EarningsEventsImport if fiscal else CustomEventsImport
    return model.model_validate(
        {
            "schema_version": "iirp.earnings-events.v1" if fiscal else "iirp.custom-events.v1",
            "research_as_of": simple.research_as_of.isoformat(),
            "company": simple.company.model_dump(mode="json"),
            "scope": scope,
            "events": events,
            "coverage": coverage,
            "warnings": simple.warnings,
        }
    )


def simple_example(kind):
    fiscal = kind == "earnings"
    event = {
        **(
            {
                "fiscal_year": 2025,
                "fiscal_quarter": 1,
                "period_start": None,
                "period_end": None,
                "period_kind": "未知",
            }
            if fiscal
            else {"name": "示例活动（请替换为查实事实）", "event_type": "product_launch"}
        ),
        "date": "2025-04-15",
        "status": "已发生",
        "sources": [
            {
                "url": "https://example.com/replace-with-real-source",
                "supports": ["日期", "已发生", *(["财年财季"] if fiscal else [])],
                "note": "仅为格式示例；请替换为已打开来源对以上事实的简短依据。",
            }
        ],
    }
    return {
        "schema_version": f"iirp.{kind}.simple.v1",
        "research_as_of": "2026-09-12",
        "company": {"name": "示例公司（请替换）", "ticker": None, "exchange_mic": None},
        "scope": {"fiscal_year_start": 2025, "fiscal_year_end": 2025, "fiscal_quarters": [1], "include_scheduled": False}
        if fiscal
        else {"year_start": 2025, "year_end": 2025, "event_types": ["product_launch"], "include_scheduled": False},
        "events": [event],
    }
