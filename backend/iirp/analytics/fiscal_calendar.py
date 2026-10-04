"""Source-backed fiscal and announcement months, separate from return samples."""

from collections import Counter
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

FieldStatus = Literal["confirmed", "missing", "unverified", "unsupported", "conflicting"]


class FiscalCalendarEntry(BaseModel):
    key: str | None = None
    fiscal_year: int | None
    quarter: str
    group: str
    period_start: str | None = None
    period_end: str | None = None
    announcement_date: str | None = None
    period_start_status: FieldStatus = "missing"
    period_end_status: FieldStatus = "missing"
    announcement_status: FieldStatus = "missing"
    reasons: list[str] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    review_note: str | None = None


class MonthCount(BaseModel):
    month: int
    n: int


class FiscalMonthRange(BaseModel):
    label: str
    n: int


class FiscalQuarterCalendar(BaseModel):
    quarter: str
    period_months: str | None = None
    period_ranges: list[FiscalMonthRange] = Field(default_factory=list)
    announcement_months: str | None = None
    common_announcement_months: list[int] = Field(default_factory=list)
    announcement_counts: list[MonthCount] = Field(default_factory=list)
    period_n: int = 0
    announcement_n: int = 0
    entries: list[FiscalCalendarEntry] = Field(default_factory=list)


class FiscalCoverage(BaseModel):
    target_years: list[int]
    current_fiscal_year: int | None
    rankings: list[dict[str, Any]]
    gaps: list[dict[str, Any]]
    definition: str
    target_reason: str | None = None
    # Absent on retained results from before this contract; never computed on GET.
    quarter_calendar: list[FiscalQuarterCalendar] | None = None


def _date(value):
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def month_ranges(months):
    """Only observed contiguous runs; December/January remains a circular run."""
    months = set(months)
    if not months:
        return None
    if len(months) == 12:
        return "1—12月"
    starts = sorted(month for month in months if (month - 2) % 12 + 1 not in months)
    labels = []
    for first in starts:
        last = first
        while last % 12 + 1 in months:
            last = last % 12 + 1
        labels.append(f"{first}月" if first == last else
                      f"{first}—{last}月" if first < last else f"{first}月—次年{last}月")
    return "、".join(labels)


def _period_months(first, last):
    start, end = _date(first), _date(last)
    if start is None or end is None or start > end:
        return set()
    # At most twelve calendar months, even for an anomalously long source period.
    count = min(12, (end.year - start.year) * 12 + end.month - start.month + 1)
    return {(start.month - 1 + offset) % 12 + 1 for offset in range(count)}


def quarter_calendar(rows, target, cutoff, *, quarters=(1, 2, 3, 4)):
    output = []
    cutoff = _date(cutoff)
    for quarter in (f"Q{value}" for value in quarters):
        selected = [row for row in rows if row.get("category") == quarter]
        entries, periods, announcements = [], [], []
        for row in selected:
            sources = [source for source in row.get("sources", [])
                       if isinstance(source, dict) and not source.get("rejected")
                       and str(source.get("url", "")).startswith(("https://", "http://"))]
            supports = {field for source in sources for field in source.get("supports", [])}
            original = row.get("anchor", {}).get("original_date")
            def status(field, value, verified):
                if field in row.get("calendar_conflicts", []) or (field == "event_date" and row.get("date_status") == "conflicting"):
                    return "conflicting"
                if _date(value) is None:
                    return "missing"
                if not verified:
                    return "unverified"
                return "confirmed" if field in supports else "unsupported"
            reasons = [reason for reason in row.get("exclusion_reasons", []) if reason in {
                "user_excluded", "unassigned_year", "not_historical_year",
                "unverified_or_nonstandard_fiscal_period", "duplicate_fiscal_period",
                "event_not_confirmed_occurred",
                "unverified_event_date", "unsupported_event_date", "outside_requested_years",
            }]
            # Calendar qualification does not depend on price completeness, exact
            # clock attribution, benchmark availability or common return years.
            if row.get("group") != "historical" and "not_historical_year" not in reasons:
                reasons.append("not_historical_year")
            if row.get("year") not in target:
                reasons.append("outside_requested_years")
            if cutoff and _date(original) and _date(original) > cutoff:
                reasons.append("future_announcement")
            entry = FiscalCalendarEntry(
                key=row["key"], fiscal_year=row.get("year"), quarter=quarter, group=row["group"],
                period_start=row.get("period_start"), period_end=row.get("period_end"),
                announcement_date=original,
                period_start_status=status("period_start", row.get("period_start"), row.get("period_verified")),
                period_end_status=status("period_end", row.get("period_end"), row.get("period_verified")),
                announcement_status=status("event_date", original, row.get("date_verified") and row.get("date_status") == "supported"),
                reasons=list(dict.fromkeys(reasons)), sources=sources, review_note=row.get("review_note"),
            )
            if not entry.reasons:
                if entry.period_start_status == entry.period_end_status == "confirmed":
                    start, end = _date(entry.period_start), _date(entry.period_end)
                    if start > end or (cutoff and end > cutoff) or (_date(original) and end > _date(original)):
                        entry.period_end_status = "conflicting"
                    else:
                        periods.append(entry)
                if entry.announcement_status == "confirmed":
                    announcements.append(entry)
            entries.append(entry)
        for year in target:
            if not any(entry.fiscal_year == year for entry in entries):
                entries.append(FiscalCalendarEntry(fiscal_year=year, quarter=quarter, group="historical", reasons=["missing_event"]))
        entries.sort(key=lambda item: (item.fiscal_year or 0, item.announcement_date or "", item.key or ""), reverse=True)
        counts = Counter(_date(entry.announcement_date).month for entry in announcements)
        ranges = Counter(month_ranges(_period_months(entry.period_start, entry.period_end)) for entry in periods)
        output.append(FiscalQuarterCalendar(
            quarter=quarter, period_months=" / ".join(ranges) or None,
            period_ranges=[FiscalMonthRange(label=label, n=count) for label, count in ranges.items()],
            announcement_months=month_ranges(counts),
            common_announcement_months=sorted(month for month, count in counts.items() if count == max(counts.values(), default=0)),
            announcement_counts=[MonthCount(month=month, n=count) for month, count in sorted(counts.items())],
            period_n=len(periods), announcement_n=len(announcements), entries=entries,
        ).model_dump())
    return output
