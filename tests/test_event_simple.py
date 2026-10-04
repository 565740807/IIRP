"""Concise external AI documents remain unapproved, evidence-carrying candidates."""

import copy
import json

import pytest
from iirp.event_contracts import parse_event_import
from iirp.event_service import event_prompt
from test_event_contracts import document


def simple_document(years=8):
    return {
        "schema_version": "iirp.earnings.simple.v1",
        "research_as_of": "2026-09-12",
        "company": {"name": "明确合成测试公司", "ticker": "AAPL"},
        "scope": {"fiscal_year_start": 2026 - years, "fiscal_year_end": 2025},
        "events": [
            {
                "fiscal_year": year,
                "fiscal_quarter": quarter,
                "date": f"{year}-{quarter * 3:02}-28",
                "status": "已发生",
                "period_kind": "常规",
                "sources": [
                    {
                        "url": f"https://example.com/synthetic/{year}/q{quarter}",
                        "supports": ["日期", "已发生", "财年财季", "财期类型"],
                        "note": "明确合成中文来源说明；并非真实财务事实。",
                    }
                ],
            }
            for year in range(2026 - years, 2026)
            for quarter in range(1, 5)
        ],
    }


def parse(value):
    return parse_event_import(json.dumps(value, ensure_ascii=False))


@pytest.mark.parametrize("years", [8, 20])
def test_concise_complete_scope_normalizes_without_repeated_coverage_or_internal_ids(years):
    result = parse(simple_document(years))
    assert result.schema_version == "iirp.earnings-events.v1"
    assert len(result.events) == len(result.coverage) == years * 4
    assert len({e.client_event_id for e in result.events}) == years * 4
    assert all(c.search_status == "partial" for c in result.coverage)
    first = result.events[0]
    assert first.event_status == "occurred" and first.date_status == "supported"
    assert first.event_time is first.timezone is first.period_start is first.period_end is None
    assert first.time_basis == "unknown" and first.time_precision == "date"
    assert first.sources[0].source_kind == "unknown"
    assert "中文来源" in first.sources[0].evidence_note
    assert "period_start" not in first.sources[0].supports
    assert parse(simple_document(years)).model_dump() == result.model_dump()


def test_omissions_never_imply_occurrence_fiscal_evidence_or_complete_search():
    value = simple_document(1)
    value["events"] = [value["events"][0]]
    event = value["events"][0]
    event.pop("status")
    event.pop("period_kind")
    event["sources"][0]["supports"] = []
    result = parse(value)
    event = result.events[0]
    assert event.event_status == event.period_kind == "unknown"
    assert event.date_status == "unverified"
    assert event.sources[0].supports == []
    assert "example.com" in event.sources[0].url
    assert [(r.search_status, r.result_status) for r in result.coverage] == [
        ("partial", "events_found"),
        *[("not_searched", "unresolved")] * 3,
    ]


def test_conflicts_and_explicit_unsearched_not_found_are_distinct():
    value = simple_document(1)
    value["events"] = [value["events"][0]]
    value["events"][0].update(date=None, date_conflict=True)
    value["events"][0]["sources"].append(
        {
            "url": "https://example.com/conflict",
            "supports": ["日期"],
            "note": "候选日期2025-03-27与2025-03-28冲突，待核对。",
        }
    )
    value["gaps"] = [
        {
            "fiscal_year": 2025,
            "fiscal_quarter": 2,
            "status": "未找到",
            "note": "已检索未找到；不代表未举办。",
        },
        {"fiscal_year": 2025, "fiscal_quarter": 3, "status": "未查询", "note": "尚未查证。"},
    ]
    result = parse(value)
    assert result.events[0].date_status == "conflicting"
    assert result.events[0].event_date is None
    assert len(result.events[0].sources) == 2
    assert result.coverage[1].result_status == "not_found"
    assert result.coverage[2].search_status == result.coverage[3].search_status == "not_searched"


@pytest.mark.parametrize(
    "edit",
    [
        lambda e: e.update(date_verified=True),
        lambda e: e.update(date_conflict=True),
        lambda e: e.update(time="16:10"),
        lambda e: e.update(release_session="盘后"),
        lambda e: e.update(period_start="2025-01-01", period_end="2025-03-01"),
        lambda e: e["sources"][0].update(supports=["所有字段"]),
        lambda e: e["sources"][0].update(url="javascript:alert(1)"),
    ],
)
def test_simple_input_rejects_forged_approval_and_unsupported_optional_claims(edit):
    value = simple_document(1)
    edit(value["events"][0])
    with pytest.raises(ValueError):
        parse(value)


def test_supported_session_and_real_period_endpoints_do_not_invent_minutes():
    value = simple_document(1)
    event = value["events"][0]
    event.update(period_start="2024-12-29", period_end="2025-03-22", release_session="盘后")
    event["sources"][0]["supports"] += ["财期起止", "公告时段"]
    parsed = parse(value).events[0]
    assert str(parsed.period_start) == "2024-12-29"
    assert str(parsed.period_end) == "2025-03-22"
    assert parsed.release_session == "after_close" and parsed.event_time is None


@pytest.mark.parametrize("kind", ["custom", "earnings"])
def test_default_prompt_embeds_short_parseable_example_and_keeps_advanced_schema(kind):
    result = event_prompt(kind)
    assert "$defs" not in result["prompt"] and "client_event_id" not in result["prompt"]
    assert "每轮最多 3 个问题" in result["prompt"] and "不能联网" in result["prompt"]
    assert "财期类型" in result["prompt"] and "来源" in result["prompt"]
    assert result["input_schema_version"] in result["prompt"]
    assert json.dumps(result["input_example"], ensure_ascii=False, indent=2) in result["prompt"]
    parsed = parse(result["input_example"])
    assert parsed.schema_version == result["schema_version"]
    assert result["json_schema"]["additionalProperties"] is False
    assert result["input_json_schema"]["additionalProperties"] is False
    assert parse(document(kind == "earnings")).schema_version == parsed.schema_version


def test_custom_minimal_date_and_source_normalizes_without_extra_fields():
    value = {
        "schema_version": "iirp.custom.simple.v1",
        "research_as_of": "2025-01-01",
        "company": {"name": "合成公司", "ticker": "AAPL"},
        "scope": {"year_start": 2024, "year_end": 2024, "event_types": ["developer_keynote"]},
        "events": [
            {
                "name": "合成活动",
                "event_type": "developer_keynote",
                "date": "2024-06-10",
                "sources": [
                    {
                        "url": "https://example.com/event",
                        "supports": ["日期"],
                        "note": "合成日期依据",
                    }
                ],
            }
        ],
    }
    result = parse(value)
    assert result.events[0].event_year == 2024
    assert result.events[0].event_status == "unknown"
    assert result.events[0].date_status == "supported"
    invalid = copy.deepcopy(value)
    invalid["events"][0]["date"] = None
    with pytest.raises(ValueError, match="year"):
        parse(invalid)


def test_simple_reordering_and_deletion_preserve_event_identity_for_revision_diff():
    value = simple_document(1)
    original = parse(value)
    value["events"] = list(reversed(value["events"][1:]))
    revised = parse(value)
    original_ids = {e.fiscal_quarter: e.client_event_id for e in original.events}
    assert [e.client_event_id for e in revised.events] == [original_ids[q] for q in (4, 3, 2)]
    assert original_ids[1] not in {e.client_event_id for e in revised.events}


def test_time_conflict_is_retained_without_qualifying_exact_reaction():
    value = simple_document(1)
    event = value["events"][0]
    event.update(time_conflict=True, time=None, time_basis="未知")
    event["sources"][0]["note"] = "来源一写盘前，来源二写盘后；日期一致，时段冲突。"
    parsed = parse(value).events[0]
    assert parsed.date_status == "supported" and parsed.time_basis == "unknown"
    assert parsed.release_session == "unknown" and "冲突" in parsed.notes
    event.update(release_session="盘后")
    with pytest.raises(ValueError, match="时间冲突"):
        parse(value)


def test_gap_conflict_cannot_be_lost_or_marked_as_resolved_candidate():
    value = simple_document(1)
    value["gaps"] = [
        {"fiscal_year": 2025, "fiscal_quarter": 1, "status": "冲突", "note": "见两份来源"}
    ]
    with pytest.raises(ValueError, match="冲突组"):
        parse(value)
    value["events"][0].update(date=None, date_conflict=True)
    parsed = parse(value)
    assert "冲突" in parsed.coverage[0].notes
    assert parsed.events[0].date_status == "conflicting"


def test_source_support_claims_match_each_event_kind():
    """A real AI first pass used earnings `事件类型` after the prompt advertised it."""
    from iirp.event_prompt_contract import schema_field_lines
    from iirp.event_simple import SimpleCustomImport, SimpleEarningsImport
    from pydantic import ValidationError

    earnings = simple_document(1)
    earnings["events"][0]["sources"][0]["supports"].append("事件类型")
    with pytest.raises(ValidationError, match="supports"):
        SimpleEarningsImport.model_validate(earnings)
    earnings["events"][0]["sources"][0]["supports"].remove("事件类型")
    assert parse(earnings).events[0].event_type == "earnings_release"

    custom = event_prompt("custom")["input_example"]
    custom["events"][0]["sources"][0]["supports"].append("财年财季")
    with pytest.raises(ValidationError, match="supports"):
        SimpleCustomImport.model_validate(custom)
    custom["events"][0]["sources"][0]["supports"].remove("财年财季")
    assert parse(custom).events[0].event_type == "product_launch"

    earnings_lines = schema_field_lines(event_prompt("earnings")["input_json_schema"])
    custom_lines = schema_field_lines(event_prompt("custom")["input_json_schema"])
    earnings_claims = next(line for line in earnings_lines if line.startswith("events[].sources[].supports:"))
    custom_claims = next(line for line in custom_lines if line.startswith("events[].sources[].supports:"))
    assert "事件类型" not in earnings_claims and "财年财季" in earnings_claims
    assert "财年财季" not in custom_claims and "事件类型" in custom_claims


@pytest.mark.parametrize("kind", ["custom", "earnings"])
def test_prompt_schema_contract_bilingual_and_real_world_extra_keys(kind):
    from iirp.event_prompt_contract import schema_field_lines
    from pydantic import ValidationError
    zh = event_prompt(kind, "zh")
    en = event_prompt(kind, "en")
    assert zh["input_json_schema"] == en["input_json_schema"]
    lines = schema_field_lines(zh["input_json_schema"])
    assert lines and all(line in zh["prompt"] and line in en["prompt"] for line in lines)
    for path in ("company.exchange_mic", "scope.include_scheduled", "events[].date", "events[].sources[].supports"):
        assert any(line.startswith(path + ":") for line in lines)
    assert "company.exchange_mic" in en["prompt"] and "company.exchange_mic" in zh["prompt"]
    assert "include_future" in zh["prompt"] and "include_future" in en["prompt"]
    if kind == "earnings":
        assert any(line.startswith("scope.fiscal_quarters:") for line in lines)
        assert "Q1—Q4" in zh["prompt"] and "Q1-Q4" in en["prompt"]
        assert zh["input_example"]["scope"]["fiscal_quarters"] == [1]
    wrong = copy.deepcopy(zh["input_example"])
    wrong["company"].update(exchange="NASDAQ", mic="XNAS", security_type="common")
    wrong["scope"]["include_future"] = False
    with pytest.raises(ValidationError) as error:
        parse(wrong)
    assert sum(issue["type"] == "extra_forbidden" for issue in error.value.errors()) == 4
