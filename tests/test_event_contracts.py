"""Import boundary examples are synthetic and never query source links."""

import copy
import json
from pathlib import Path

import pytest
from iirp.event_contracts import (
    CustomEventsImport,
    EarningsEventsImport,
    import_warnings,
    parse_event_import,
)
from pydantic import ValidationError


def document(earnings=False):
    source = {
        "url": "https://example.com/event",
        "title": "Synthetic evidence",
        "publisher": "Synthetic company",
        "published_date": "2024-06-10",
        "source_kind": "primary",
        "supports": ["event_date", "event_status"],
        "evidence_note": "Synthetic fixture confirms this event date and occurrence.",
    }
    event = {
        "client_event_id": "event-2024",
        "event_name": "Synthetic event 2024",
        "event_type": "developer_keynote",
        "event_year": 2024,
        "event_date": "2024-06-10",
        "event_time": None,
        "timezone": None,
        "time_precision": "date",
        "time_basis": "unknown",
        "event_status": "occurred",
        "date_status": "supported",
        "sources": [source],
        "notes": None,
    }
    scope = {
        "year_start": 2024,
        "year_end": 2024,
        "event_types": ["developer_keynote"],
        "include_keywords": [],
        "exclude_keywords": [],
        "anchor_basis": "event_start",
        "include_scheduled": False,
    }
    coverage = {
        "year": 2024,
        "event_type": "developer_keynote",
        "search_status": "searched",
        "result_status": "events_found",
        "event_ids": ["event-2024"],
        "source_urls": ["https://example.com/event"],
        "notes": "Synthetic complete query.",
    }
    if earnings:
        event.update(
            {
                "event_type": "earnings_release",
                "fiscal_year": 2023,
                "fiscal_quarter": 4,
                "period_end": "2023-12-31",
                "period_kind": "regular",
                "release_session": "unknown",
            }
        )
        scope = {
            "fiscal_year_start": 2023,
            "fiscal_year_end": 2023,
            "fiscal_quarters": [4],
            "anchor_basis": "earnings_release",
            "include_scheduled": False,
        }
        coverage.pop("year")
        coverage.pop("event_type")
        coverage.update({"fiscal_year": 2023, "fiscal_quarter": 4})
    return {
        "schema_version": "iirp.earnings-events.v1" if earnings else "iirp.custom-events.v1",
        "research_as_of": "2025-01-01",
        "company": {"name": "Synthetic Company", "ticker": "AAPL", "exchange_mic": "XNAS"},
        "scope": scope,
        "events": [event],
        "coverage": [coverage],
        "warnings": [],
    }


def parse(payload):
    return parse_event_import(json.dumps(payload))


def test_document_accepts_date_only_without_inventing_time_or_verification():
    result = parse(document())
    assert isinstance(result, CustomEventsImport)
    assert result.events[0].event_time is None
    assert "verified" not in result.events[0].model_dump()
    assert any("待核对" in warning for warning in import_warnings(result))
    fence = chr(96) * 3
    assert (
        parse_event_import("\n" + fence + "json\n" + json.dumps(document()) + "\n" + fence)
        == result
    )
    assert CustomEventsImport.model_validate_json(json.dumps(document())) == result


def test_checked_in_custom_json_example_obeys_authoritative_contract():
    example = (Path(__file__).parent / "fixtures/custom-events-example.json").read_text()
    assert '"iirp.custom-events.v1"' in example
    assert isinstance(parse_event_import(example), CustomEventsImport)


@pytest.mark.parametrize(
    "field,value",
    [
        ("verified", True),
        ("date_verified", True),
        ("price", "100"),
        ("event_time", "00:00"),
        ("event_date", "2024-02-30"),
        ("event_date", "2024-6-10"),
        ("event_date", 1717977600),
        ("event_date", "2024-06-10T00:00:00"),
        ("event_year", True),
        ("event_year", "2024"),
        ("event_year", 2023),
        ("time_precision", "exact"),
        ("time_basis", "verified"),
        ("timezone", "PDT"),
        ("timezone", "EST"),
        ("timezone", "Not/AZone"),
        ("event_name", " "),
        ("event_status", "verified"),
        ("date_status", "verified"),
        ("date_status", "conflicting"),
    ],
)
def test_invalid_fields_and_forged_verification_are_rejected(field, value):
    data = document()
    data["events"][0][field] = value
    with pytest.raises((ValidationError, ValueError)):
        parse(data)


def test_unknown_top_field_version_and_non_boolean_scope_are_rejected():
    for edit in (
        lambda d: d.update({"approved": True}),
        lambda d: d.update({"schema_version": "iirp.custom-events.v2"}),
        lambda d: d["scope"].update({"include_scheduled": 1}),
        lambda d: d["scope"].update({"anchor_basis": "first_announcement"}),
    ):
        data = document()
        edit(data)
        with pytest.raises((ValidationError, ValueError)):
            parse(data)


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///tmp/source",
        "//example.com/x",
        "https://u:password@example.com/x",
        "https://example.com:bad/x",
        "https://",
        "https://example.com/has space",
        "https://example.com/\n",
    ],
)
def test_sources_reject_non_http_and_malformed_urls(url):
    data = document()
    data["events"][0]["sources"][0]["url"] = url
    with pytest.raises((ValidationError, ValueError)):
        parse(data)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d["events"][0].update({"sources": []}),
        lambda d: d["events"][0]["sources"][0].update({"supports": ["event_date"]}),
        lambda d: d["events"][0]["sources"][0].update({"supports": ["event_status"]}),
        lambda d: d["events"][0]["sources"][0].update({"supports": ["event_date", "event_date"]}),
    ],
)
def test_date_and_occurrence_require_separate_source_support(mutation):
    data = document()
    mutation(data)
    with pytest.raises(ValidationError):
        parse(data)


def minute(data, day="2024-06-10", instant="10:00", zone="America/Los_Angeles"):
    row = data["events"][0]
    row.update(
        {
            "event_date": day,
            "event_time": instant,
            "timezone": zone,
            "time_precision": "minute",
            "time_basis": "official_schedule",
        }
    )
    row["sources"][0]["supports"] += ["event_time", "timezone"]
    return data


def test_schedule_minute_survives_as_schedule_with_independent_occurrence_evidence():
    result = parse(minute(document()))
    assert result.events[0].time_basis == "official_schedule"
    assert result.events[0].event_time == "10:00"
    assert any("日程" in warning for warning in import_warnings(result))


@pytest.mark.parametrize("day,instant", [("2024-03-10", "02:30"), ("2024-11-03", "01:30")])
def test_nonexistent_and_ambiguous_dst_minutes_are_not_silently_converted(day, instant):
    with pytest.raises(ValidationError, match="夏令时"):
        parse(minute(document(), day, instant, "America/New_York"))


def test_missing_date_requires_unknown_time_but_preserves_conflict_sources():
    data = document()
    row = data["events"][0]
    row.update({"event_date": None, "time_precision": "unknown", "date_status": "conflicting"})
    data["coverage"][0]["result_status"] = "unresolved"
    assert parse(data).events[0].event_date is None
    row["time_precision"] = "date"
    with pytest.raises(ValidationError):
        parse(data)


@pytest.mark.parametrize(
    "edit",
    [
        lambda d: d["scope"].update({"year_end": 2025}),
        lambda d: d["coverage"].append(copy.deepcopy(d["coverage"][0])),
        lambda d: d["coverage"][0].update({"event_ids": ["does-not-exist"]}),
        lambda d: d["coverage"][0].update({"event_ids": ["event-2024", "event-2024"]}),
        lambda d: d["coverage"][0].update({"event_ids": [], "result_status": "unresolved"}),
        lambda d: d["coverage"][0].update({"year": 2023}),
        lambda d: d["events"].append(copy.deepcopy(d["events"][0])),
        lambda d: d["scope"].update({"event_types": ["developer_keynote", "developer_keynote"]}),
        lambda d: d["scope"].update({"year_start": 2025}),
    ],
)
def test_coverage_grid_ids_and_cross_references_must_match(edit):
    data = document()
    edit(data)
    with pytest.raises(ValidationError):
        parse(data)


def test_no_event_years_remain_explicit_and_confirmed_none_needs_evidence():
    data = document()
    data["events"] = []
    data["coverage"][0].update({"event_ids": [], "result_status": "not_found", "source_urls": []})
    assert parse(data).events == []
    data["coverage"][0]["result_status"] = "confirmed_none"
    with pytest.raises(ValidationError):
        parse(data)
    data["coverage"][0]["source_urls"] = ["https://example.com/not-held"]
    assert parse(data).coverage[0].result_status == "confirmed_none"


def test_future_dates_cannot_be_occurred_or_sneak_into_excluded_plans():
    data = document()
    data["research_as_of"] = "2024-06-01"
    with pytest.raises(ValidationError):
        parse(data)
    data["events"][0]["event_status"] = "scheduled"
    with pytest.raises(ValidationError):
        parse(data)
    data["scope"]["include_scheduled"] = True
    assert parse(data).events[0].event_status == "scheduled"


def test_earnings_keeps_fiscal_year_separate_and_accepts_evidence_backed_period_without_minute():
    data = document(earnings=True)
    row = data["events"][0]
    row["release_session"] = "after_close"
    row["sources"][0]["supports"].append("release_session")
    parsed = parse(data)
    assert isinstance(parsed, EarningsEventsImport)
    assert parsed.events[0].event_year == 2024
    assert parsed.events[0].fiscal_year == 2023
    assert parsed.events[0].event_time is None
    assert parsed.events[0].release_session == "after_close"


@pytest.mark.parametrize(
    "field,value",
    [
        ("release_session", "after_close"),
        ("fiscal_quarter", 5),
        ("period_end", "2024-07-01"),
        ("fiscal_year", 2024),
        ("event_year", None),
    ],
)
def test_earnings_rejects_unsupported_session_period_and_cross_field_conflicts(field, value):
    data = document(earnings=True)
    data["events"][0][field] = value
    with pytest.raises(ValidationError):
        parse(data)


def test_earnings_unassigned_transition_stays_unresolved_and_is_not_assigned_by_guess():
    data = document(earnings=True)
    row = data["events"][0]
    row.update({"fiscal_quarter": None, "period_kind": "transition"})
    data["coverage"][0].update({"event_ids": [], "result_status": "unresolved"})
    data["warnings"] = ["Transition fiscal quarter unknown."]
    parsed = parse(data)
    assert parsed.events[0].fiscal_quarter is None
    data["coverage"][0]["result_status"] = "not_found"
    with pytest.raises(ValidationError):
        parse(data)


@pytest.mark.parametrize(
    "raw",
    [
        '{"schema_version":"iirp.custom-events.v1","schema_version":"iirp.earnings-events.v1"}',
        '{"value":NaN}',
        '{"value":Infinity}',
        "[]",
        '{"a":1} trailing',
    ],
)
def test_duplicate_json_keys_constants_and_trailing_prose_are_rejected(raw):
    with pytest.raises(ValueError):
        parse_event_import(raw)


def test_non_string_version_is_a_user_validation_error():
    data = document()
    data["schema_version"] = []
    with pytest.raises(ValueError, match="版本"):
        parse(data)


def test_same_fiscal_period_candidates_are_visible_for_review_not_silently_merged():
    data = document(earnings=True)
    second = copy.deepcopy(data["events"][0])
    second.update({"client_event_id": "second-release", "event_name": "Possible duplicate release"})
    data["events"].append(second)
    data["coverage"][0]["event_ids"].append("second-release")
    parsed = parse(data)
    assert len(parsed.events) == 2
    assert any("多个主发布" in warning for warning in import_warnings(parsed))


def test_native_period_kind_correction_requires_explicit_source_location():
    from iirp.contracts import EventCorrection
    from pydantic import ValidationError

    values = {
        "fiscal_year": 2024, "fiscal_quarter": 2, "announced_date": "2024-05-02",
        "time_precision": "date_only", "source_url": "https://example.com/official",
        "note": "Synthetic date review", "revision": 1,
    }
    assert EventCorrection.model_validate(values).period_kind is None
    with pytest.raises(ValidationError, match="来源链接及原文或明确位置"):
        EventCorrection.model_validate({**values, "period_kind": "regular"})
    corrected = EventCorrection.model_validate({
        **values, "period_kind": "regular",
        "period_kind_source_url": "https://example.com/synthetic-filing",
        "period_kind_evidence": "Synthetic filing, first paragraph: ordinary fiscal quarter.",
    })
    assert corrected.period_kind == "regular"


def test_event_analysis_year_lists_cannot_repeat_coverage_denominator():
    from iirp.event_api import EventAnalysisInput
    from pydantic import ValidationError

    base = {"request_id": "synthetic", "version": 1}
    with pytest.raises(ValidationError, match="指定历史年份不能重复"):
        EventAnalysisInput.model_validate({**base, "years": [2024, 2024]})
    with pytest.raises(ValidationError, match="排除历史年份不能重复"):
        EventAnalysisInput.model_validate({**base, "excluded_years": [2024, 2024]})
    assert EventAnalysisInput.model_validate({**base, "years": [2024, 2025]}).years == [2024, 2025]
