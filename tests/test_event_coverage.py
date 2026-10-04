"""Saved source coverage must survive return and acquisition eligibility filters."""

import csv
import io
import json
from datetime import date

import pytest
from iirp import event_service as service
from test_event_import_capacity import many_years
from test_event_service import analysis, confirmation, count_acquisition_jobs, plan, preview
from test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_database,
    seed_prices,
    seed_security,
)


def imported(*, pending=True, excluded=False):
    payload = many_years()
    command = confirmation(preview(payload))
    for review in command["reviews"]:
        review.update(date_verified=True, period_verified=True)
    command["reviews"][-1].update(
        date_verified=not pending, period_verified=not pending,
        selected=not excluded, note="合成资料：保留待核对或主动排除的原记录",
    )
    return payload, service.confirm_import(command)


def q4_gap(data, year=2023, window="after5"):
    return next(g for g in data["fiscal_coverage"]["gaps"]
                if g["year"] == year and g["quarter"] == "Q4" and g["window"] == window)


def test_pending_source_preserved_without_observation_prices_or_extra_n_and_frozen_after_review():
    security = seed_security()
    seed_prices(security, date(2015, 12, 1), date(2025, 1, 1))
    payload, saved = imported()
    pending = payload["events"][-1]
    created = analysis(saved, current_fiscal_year=2024)
    old = service.get_analysis(created["analysis_id"])
    data = old["data"]
    for window in ("before5", "day0", "after5", "through5"):
        gap = q4_gap(data, window=window)
        assert gap["status"] == "unverified_event_date"
        assert "unverified_or_nonstandard_fiscal_period" in gap["reasons"]
        assert gap["event_keys"] == [pending["client_event_id"]]
        distribution = next(d for d in data["distributions"] if d["key"] == f"Q4:{window}")
        assert distribution["stock"]["n"] == 7
        sample = next(s for s in distribution["samples"] if s["key"] == pending["client_event_id"])
        assert sample["eligible"] is False and sample["stock"] is None
        assert sample["status"] == gap["status"]
    assert pending["client_event_id"] not in {r["key"] for r in data["rows"]}
    assert pending["client_event_id"] not in {r["key"] for r in data["series"]}
    calendar = next(q for q in data["fiscal_coverage"]["quarter_calendar"] if q["quarter"] == "Q4")
    entry = next(e for e in calendar["entries"] if e["fiscal_year"] == 2023)
    assert entry["announcement_status"] == "unverified"
    assert entry["announcement_date"] == pending["event_date"]
    assert entry["sources"] == pending["sources"]
    assert "unverified_event_date" in entry["reasons"]
    assert "missing_event" not in entry["reasons"]
    assert calendar["announcement_n"] == calendar["period_n"] == 7
    old_csv = service.export_analysis(created["analysis_id"], old["result_id"], "csv")
    fiscal = next(row for row in csv.DictReader(io.StringIO(old_csv)) if row["record_type"] == "fiscal_coverage")
    assert json.loads(fiscal["statistics_or_window"]) == data["fiscal_coverage"]
    revised_command = confirmation(preview(payload, set_id=saved["set_id"], expected_version=1),
                                   expected_version=1, revision_note="合成资料逐项复核通过")
    for review in revised_command["reviews"]:
        review.update(date_verified=True, period_verified=True)
    revised = service.confirm_import(revised_command)
    newer = service.get_analysis(analysis(revised, current_fiscal_year=2024)["analysis_id"])
    assert q4_gap(newer["data"])["status"] == "available"
    assert next(d for d in newer["data"]["distributions"] if d["key"] == "Q4:after5")["stock"]["n"] == 8
    assert service.get_analysis(created["analysis_id"], old["result_id"])["data"] == data
    assert service.export_analysis(created["analysis_id"], old["result_id"], "csv") == old_csv
    assert count_acquisition_jobs() == 0


@pytest.mark.parametrize("excluded", [False, True])
def test_hidden_fiscal_records_remain_coverage_but_never_request_prices(excluded):
    seed_security()
    payload = many_years(1)
    command = confirmation(preview(payload))
    for review in command["reviews"]:
        review.update(selected=not excluded, note="合成资料：主动排除" if excluded else "待核对")
    saved = service.confirm_import(command)
    created = analysis(saved, current_fiscal_year=2024)
    plan(created["analysis_id"])
    data = service.get_analysis(created["analysis_id"])["data"]
    assert count_acquisition_jobs() == 0 and data["rows"] == [] and data["series"] == []
    gap = q4_gap(data)
    assert gap["status"] == ("user_excluded" if excluded else "unverified_event_date")
    assert all(g["year"] != 2016 for g in data["fiscal_coverage"]["gaps"])
    # An unimported year must never masquerade as a researched missing event.
    with pytest.raises(ValueError, match="超出导入事件范围"):
        analysis(saved, current_fiscal_year=2024, years=[2016])
    assert all(d["stock"]["n"] == 0 for d in data["distributions"])


def test_missing_prices_and_unformed_window_are_not_fact_review_failures():
    seed_security()
    _, saved = imported(pending=False)
    data = service.get_analysis(analysis(saved, current_fiscal_year=2024, cutoff_date="2023-12-21")["analysis_id"])["data"]
    assert q4_gap(data, 2022)["status"] == "incomplete_path"
    assert q4_gap(data)["status"] == "not_yet_formed"
    assert not {"missing_event", "unverified_event_date"}.intersection(q4_gap(data)["reasons"])


def test_research_year_exclusion_retains_reviewed_source_and_does_not_call_it_user_rejected():
    seed_security()
    _, saved = imported(pending=False)
    data = service.get_analysis(analysis(saved, current_fiscal_year=2024, excluded_years=[2023])["analysis_id"])["data"]
    assert 2023 not in data["fiscal_coverage"]["target_years"]
    assert all(r["year"] != 2023 for r in data["rows"])
    calendar = next(q for q in data["fiscal_coverage"]["quarter_calendar"] if q["quarter"] == "Q4")
    entry = next(e for e in calendar["entries"] if e["fiscal_year"] == 2023)
    assert entry["announcement_status"] == "confirmed"
    assert entry["reasons"] == ["outside_requested_years"]
    assert entry["sources"][0]["url"] == "https://example.com/fixture-2023-q4"
