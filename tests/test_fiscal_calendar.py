"""Source-backed fiscal months, independent of price qualification (synthetic)."""

from copy import deepcopy
from datetime import date

import pytest
from iirp.analytics.event_dates import analyze_event_dates
from iirp.analytics.research import compute_research


def release(year=2024, quarter=1, day="2023-09-30", start="2023-06-01", end="2023-08-31", **changes):
    return {
        "client_event_id": f"fy{year}q{quarter}", "event_name": f"Synthetic FY{year} Q{quarter}",
        "event_type": "earnings_release", "event_date": day, "event_year": int(day[:4]),
        "fiscal_year": year, "fiscal_quarter": quarter, "period_start": start,
        "period_end": end, "period_kind": "regular", "date_status": "supported",
        "event_status": "occurred", "date_verified": True, "period_verified": True,
        "sources": [{"url": f"https://example.com/fy{year}q{quarter}", "title": "Synthetic release",
                     "supports": ["event_date", "fiscal_year", "fiscal_quarter", "period_start", "period_end"]}],
        **changes,
    }


def observed(events, **metadata):
    return analyze_event_dates(events, [], cutoff=date(2025, 9, 30), current_year=2025,
                               current_fiscal_year=2026, metadata={"requested_fiscal_years": [2024, 2025], **metadata})


def quarter(result, name="Q1"):
    return next(item for item in result["fiscal_coverage"]["quarter_calendar"] if item["quarter"] == name)


def test_noncalendar_months_cross_month_announcements_keep_original_weekend_without_prices():
    result = observed([release(), release(2025, day="2024-10-01", start="2024-06-01", end="2024-08-31")])
    item = quarter(result)
    assert item["period_months"] == "6—8月"
    assert item["announcement_months"] == "9—10月"
    assert item["period_n"] == item["announcement_n"] == 2
    assert item["common_announcement_months"] == [9, 10]
    assert item["announcement_counts"] == [{"month": 9, "n": 1}, {"month": 10, "n": 1}]
    assert item["entries"][1]["announcement_date"] == "2023-09-30"  # Saturday, never D0 Oct 2.
    assert result["rows"][0]["anchor"]["anchor_date"] == "2023-10-02"
    assert result["effective_n"] == 0
    assert all(r["statistics"]["n"] == 0 for r in result["fiscal_coverage"]["rankings"])


def test_wraparound_months_and_53_week_endpoints_are_not_calendar_quarters():
    events = [release(2024, 2, "2023-12-30", "2023-09-24", "2023-12-23"),
              release(2025, 2, "2025-01-03", "2024-09-22", "2024-12-28")]
    item = quarter(observed(events), "Q2")
    assert item["announcement_months"] == "12月—次年1月"
    assert item["period_months"] == "9—12月"
    assert [(e["period_start"], e["period_end"]) for e in item["entries"]] == [
        ("2024-09-22", "2024-12-28"), ("2023-09-24", "2023-12-23")]
    split = quarter(observed([release(), release(2025, day="2024-12-01")]))
    assert split["announcement_months"] == "9月、12月"  # Do not invent October/November.


def test_field_support_and_review_are_independent_and_missing_years_are_explicit():
    item = release(period_start=None)
    item["sources"][0]["supports"].remove("period_start")
    result = quarter(observed([item]))
    assert result["period_n"] == 0 and result["announcement_n"] == 1
    assert result["period_months"] is None
    assert result["entries"][1]["period_start_status"] == "missing"
    assert result["entries"][0]["fiscal_year"] == 2025
    assert result["entries"][0]["reasons"] == ["missing_event"]
    no_support = deepcopy(item)
    no_support["sources"][0]["supports"].remove("period_end")
    assert quarter(observed([no_support]))["entries"][1]["period_end_status"] == "unsupported"
    no_review = deepcopy(item)
    no_review["date_verified"] = False
    assert quarter(observed([no_review]))["announcement_n"] == 0


@pytest.mark.parametrize("changes", [
    {"excluded": True}, {"period_verified": False}, {"period_kind": "transition"},
    {"event_status": "scheduled"}, {"fiscal_year": 2026},
    {"event_date": "2025-10-01"},
])
def test_nonhistorical_or_unqualified_facts_remain_visible_without_month_or_ranking_n(changes):
    result = observed([release(**changes)])
    item = quarter(result)
    assert item["period_n"] == item["announcement_n"] == 0
    assert any(e["key"] == "fy2024q1" for e in item["entries"])
    assert all(r["statistics"]["n"] == 0 for r in result["fiscal_coverage"]["rankings"])


def test_duplicate_primary_dates_are_visible_but_do_not_vote_twice_for_common_month():
    result = quarter(observed([release(), release(client_event_id="duplicate", day="2023-10-02")]))
    assert result["period_n"] == result["announcement_n"] == 0
    assert len([e for e in result["entries"] if e["key"]]) == 2
    assert all("duplicate_fiscal_period" in e["reasons"] for e in result["entries"] if e["key"])


def test_automatic_and_imported_facts_share_calendar_without_changing_financial_results():
    raw = release()
    source = {"source_url": raw["sources"][0]["url"], "fiscal_year": 2024, "fiscal_quarter": 1,
              "announced_date": raw["event_date"], "period_start": raw["period_start"], "period_end": raw["period_end"],
              "period_kind": "regular", "period_kind_evidence": "Synthetic source says regular quarter."}
    event = {"id": raw["client_event_id"], "fiscal_year": 2024, "fiscal_quarter": 1,
             "announced_date": raw["event_date"], "verified": True, "time_precision": "date_only", "evidence": [source]}
    result = compute_research({"kind": "earnings", "years": [2024, 2025], "current_fiscal_year": 2026},
                              [], [event], today=date(2025, 9, 30))
    assert result["fiscal_coverage"]["quarter_calendar"] == result["date_observation"]["fiscal_coverage"]["quarter_calendar"]
    assert quarter(result)["period_months"] == "6—8月"
    assert quarter(result)["announcement_months"] == "9月"
    assert result["effective_n"] == result["date_observation"]["effective_n"] == 0
    event["evidence"].append({**source, "period_end": "2023-09-01"})
    conflict = compute_research({"kind": "earnings", "years": [2024], "current_fiscal_year": 2026},
                                [], [event], today=date(2025, 9, 30))
    assert quarter(conflict)["period_n"] == 0
    assert quarter(conflict)["entries"][0]["period_end_status"] == "conflicting"
    assert quarter(conflict)["announcement_n"] == 1


def test_rejected_other_fiscal_sources_and_previous_manual_values_cannot_attest_current_fields():
    raw = release()
    valid = {"source_url": "https://example.com/correct", "fiscal_year": 2024, "fiscal_quarter": 1,
             "announced_date": raw["event_date"], "period_end": raw["period_end"],
             "period_kind": "regular", "period_kind_evidence": "Synthetic source says regular quarter."}
    event = {"id": "source-selection", "fiscal_year": 2024, "fiscal_quarter": 1,
             "announced_date": raw["event_date"], "verified": True, "time_precision": "date_only", "evidence": [
                 valid, {**valid, "period_start": raw["period_start"], "rejected": True},
                 {**valid, "fiscal_year": 2023, "period_start": raw["period_start"]},
                 {"source_url": "https://example.com/manual", "provider": "manual_review", "previous": {**valid, "period_start": raw["period_start"]}},
             ]}
    result = compute_research({"kind": "earnings", "years": [2024], "current_fiscal_year": 2026},
                              [], [event], today=date(2025, 9, 30))
    item = quarter(result)
    assert item["announcement_n"] == 1 and item["period_n"] == 0
    assert item["entries"][0]["period_start"] is None
    assert item["entries"][0]["period_start_status"] == "missing"


def test_fiscal_rule_changes_keep_separate_ranges_and_same_url_preserves_field_attribution():
    item = quarter(observed([release(), release(2025, day="2024-12-15", start="2024-09-01", end="2024-11-30")]))
    assert set(item["period_months"].split(" / ")) == {"6—8月", "9—11月"}
    assert item["period_ranges"] == [{"label": "6—8月", "n": 1}, {"label": "9—11月", "n": 1}]
    from iirp.analytics.research import earnings_date_event
    adapted = earnings_date_event({"id": "manual", "fiscal_year": 2025, "fiscal_quarter": 1,
        "announced_date": "2024-10-01", "verified": True, "evidence": [
            {"source_url": "https://example.com/release", "fiscal_year": 2024, "fiscal_quarter": 1,
             "announced_date": "2023-09-30", "note": "旧来源摘要"},
            {"source_url": "https://example.com/release", "fiscal_year": 2025, "fiscal_quarter": 1,
             "announced_date": "2024-10-01", "note": "此次来源摘要"},
        ]}, date(2025, 9, 30))
    assert adapted["sources"][0]["supports"] == []
    assert adapted["sources"][1]["supports"] == ["event_date"]


def test_excluded_years_remove_calendar_votes_and_window_or_price_gaps_do_not_change_dates():
    events = []
    for year, month in [(2023, 9), (2024, 9), (2025, 10)]:
        day = f"{year - 1}-{month:02}-01"
        events.append({"id": str(year), "fiscal_year": year, "fiscal_quarter": 1, "announced_date": day,
                       "verified": True, "time_precision": "date_only", "evidence": [{
                           "source_url": f"https://example.com/{year}", "announced_date": day,
                           "fiscal_year": year, "fiscal_quarter": 1,
                           "period_start": f"{year - 1}-06-01", "period_end": f"{year - 1}-08-31",
                           "period_kind": "regular", "period_kind_evidence": "Synthetic source says regular quarter."}]})
    params = {"kind": "earnings", "years": [2023, 2024, 2025], "current_fiscal_year": 2026}
    first = compute_research(params, [], events, today=date(2025, 9, 30))
    second = compute_research({**params, "window": 20, "date_window": "before5", "common_years": True},
                              [], events, today=date(2025, 9, 30))
    assert first["fiscal_coverage"]["quarter_calendar"] == second["fiscal_coverage"]["quarter_calendar"]
    assert quarter(first)["common_announcement_months"] == [9]
    excluded = compute_research({**params, "excluded_years": [2025]}, [], events, today=date(2025, 9, 30))
    assert quarter(excluded)["announcement_n"] == 2
    assert quarter(excluded)["announcement_months"] == "9月"
    assert not any(e["fiscal_year"] == 2025 for e in quarter(excluded)["entries"])
