"""Automatic earnings keep date observations separate from precise R=1 returns."""

from datetime import date

import pytest
from iirp.analytics.calendar import session_window, sessions
from iirp.analytics.research import compute_research, plan_scope


def event(**changes):
    return {
        "id": "release-2023-q1",
        "fiscal_year": 2023,
        "fiscal_quarter": 1,
        "announced_date": "2024-06-10",
        "announced_at": None,
        "time_precision": "date_only",
        "verified": True,
        "fiscal_period_verified": True,
        "precise_time_supported": False,
        "is_estimate": False,
        "is_primary": True,
        "status": "DATE_VERIFIED",
        "evidence": [
            {
                "provider": "SEC release",
                "source_url": "https://example.com/actual-release",
                "period_end": "2024-03-31",
                "fiscal_year": 2023,
                "fiscal_quarter": 1,
                "announced_date": "2024-06-10",
                "excerpt": "Synthetic actual release evidence.",
            }
        ],
        **changes,
    }


def sourced_regular(**changes):
    item = event(**changes)
    source = dict(item["evidence"][0])
    source.update(
        fiscal_year=item["fiscal_year"], fiscal_quarter=item["fiscal_quarter"],
        period_kind="regular", period_kind_evidence="Synthetic filing states ordinary fiscal quarter.",
    )
    item["evidence"] = [source]
    return item


def compute(events=None, today=date(2024, 6, 30), **params):
    return compute_research(
        {"kind": "earnings", "years": [2023], "current_fiscal_year": 2024, "quarter": 1, **params},
        [
            {"date": str(day), "close": "100", "open": "100", "status": "VALID"}
            for day in sessions(date(2023, 11, 1), date(2024, 8, 31))
        ],
        events if events is not None else [event()],
        today=today,
    )


def date_row(result):
    return result["date_observation"]["rows"][0]


def test_default_date_observation_survives_public_serialization_and_precise_n_remains_zero():
    result = compute()
    observed = result["date_observation"]
    row = date_row(result)
    assert len(row["points"]) == 11
    assert row["anchor"]["original_date"] == "2024-06-10"
    assert row["anchor"]["anchor_date"] == "2024-06-10"
    assert row["baseline_extra_date"] == "2024-05-31"
    assert row["period_end"] == "2024-03-31"
    assert row["sources"][0]["url"] == "https://example.com/actual-release"
    assert observed["effective_n"] == 0  # FY/Q alone never proves a regular period.
    assert result["effective_n"] == 0
    assert row["period_kind"] == "unknown"
    assert result["rows"][0]["reaction_date"] == "2024-06-10"
    assert row["event_time"] is None
    assert row["time_verified"] is False


def test_explicit_same_period_source_can_qualify_regular_date_observation():
    result = compute([sourced_regular()])
    assert result["date_observation"]["effective_n"] == 1
    assert date_row(result)["period_kind"] == "regular"
    source = date_row(result)["sources"][0]
    assert "period_kind" in source["supports"]
    assert source["period_kind_evidence"] == "Synthetic filing states ordinary fiscal quarter."
    assert "财期类型依据" in source["evidence_note"]
    assert result["effective_n"] == 0  # No first-publication time.


def test_date_only_ignores_even_aware_provider_clock_placeholders():
    row = date_row(compute([event(announced_at="2024-06-11T00:00:00+00:00")]))
    assert row["event_time"] is None
    assert row["anchor"]["exchange_date"] == "2024-06-10"
    assert row["anchor"]["release_session"] == "unknown"


@pytest.mark.parametrize(
    "precision,session",
    [
        ("before_open", "before_open"),
        ("after_close", "after_close"),
        ("intraday", "during_session"),
    ],
)
def test_known_sessions_keep_null_minutes_and_separate_d0_from_reaction(precision, session):
    supplied = event(time_precision=precision, precise_time_supported=precision != "intraday")
    if precision in {"before_open", "after_close"}:
        supplied["evidence"][0].update(time_precision=precision, time_evidence=f"Synthetic issuer released earnings {precision}")
    result = compute([supplied])
    row = date_row(result)
    assert row["event_time"] is None
    assert row["anchor"]["release_session"] == ("unknown" if precision == "intraday" else session)
    assert row["anchor"]["anchor_date"] == "2024-06-10"
    if precision == "after_close":
        assert result["rows"][0]["reaction_date"] == "2024-06-11"
        assert any("D+1" in warning for warning in row["anchor"]["warnings"])
    elif precision == "before_open":
        assert result["rows"][0]["windows"]["5"]["end_date"] != row["windows"]["after5"]["end_date"]


def test_actual_timestamp_converts_utc_to_exchange_date_and_preserves_exact_reaction():
    supplied = event(
        announced_at="2024-06-10T20:30:00+00:00",
        time_precision="exact",
        precise_time_supported=True,
    )
    supplied["evidence"][0].update(announced_at="2024-06-10T20:30:00+00:00", time_precision="exact", time_evidence="Synthetic source: actual results released 4:30 p.m. ET")
    result = compute([supplied])
    row = date_row(result)
    assert row["event_time"] == "16:30"
    assert row["timezone"] == "America/New_York"
    assert row["anchor"]["release_session"] == "after_close"
    assert row["anchor"]["anchor_date"] == "2024-06-10"
    assert result["rows"][0]["reaction_date"] == "2024-06-11"


@pytest.mark.parametrize(
    "changes",
    [
        {"verified": False, "fiscal_period_verified": False, "status": "CANDIDATE"},
        {"is_estimate": True},
        {"fiscal_period_verified": False},
        {"is_primary": False},
        {"evidence": [{**sourced_regular()["evidence"][0], "period_kind": "transition",
                        "period_kind_evidence": "Synthetic filing states transition period."}]},
    ],
)
def test_candidate_estimate_exclusion_or_period_uncertainty_cannot_become_history_n(changes):
    result = compute([event(**changes)])
    assert date_row(result)["points"]
    assert result["date_observation"]["effective_n"] == 0
    assert not date_row(result)["windows"]["after5"]["eligible"]


def test_candidate_without_fiscal_identity_is_visible_as_unassigned_not_guessed():
    row = date_row(
        compute(
            [
                event(
                    fiscal_year=None,
                    fiscal_quarter=None,
                    fiscal_period_verified=False,
                    verified=False,
                    status="CANDIDATE",
                )
            ]
        )
    )
    assert row["group"] == "observation"
    assert row["year"] is None
    assert row["points"]
    assert not row["eligible"]


def test_current_fiscal_year_and_selected_quarter_filter_do_not_follow_release_calendar_year():
    result = compute(
        [
            sourced_regular(),
            sourced_regular(id="current", fiscal_year=2024),
            sourced_regular(id="other-quarter", fiscal_quarter=2),
            sourced_regular(id="not-selected-year", fiscal_year=2022),
        ]
    )
    rows = result["date_observation"]["rows"]
    assert [row["key"] for row in rows] == ["release-2023-q1", "current", "other-quarter"]
    assert rows[-1]["category"] == "Q2"  # All quarters retained for ranking; no relabelling as Q1.
    assert [row["group"] for row in rows] == ["historical", "current", "historical"]
    assert rows[0]["year"] == 2023 and rows[0]["event_year"] == 2024
    assert result["date_observation"]["effective_n"] == 2
    assert {s["category"]: s["effective_n"] for s in result["date_observation"]["category_summary"]} == {"Q1": 1, "Q2": 1}


def test_conflicting_date_and_naive_exact_clock_never_invent_reliable_minute():
    conflict = date_row(compute([event(time_precision="conflict", status="CONFLICT")]))
    assert conflict["anchor"]["anchor_date"] is None
    assert conflict["points"] == []
    naive = date_row(
        compute(
            [
                event(
                    announced_at="2024-06-10T16:30:00",
                    time_precision="exact",
                    precise_time_supported=True,
                )
            ]
        )
    )
    assert naive["event_time"] is None
    assert not naive["time_verified"]


def test_frozen_cutoff_is_shared_and_later_prices_cannot_complete_d_plus_5():
    result = compute(today=date(2024, 6, 12))
    row = date_row(result)
    assert (
        result["metadata"]["cutoff_date"] == result["date_observation"]["metadata"]["cutoff_date"]
    )
    assert row["windows"]["before5"]["complete"]
    assert row["windows"]["after5"]["status"] == "not_yet_formed"
    assert row["points"][-1]["close"] is None


@pytest.mark.parametrize("precision", ["before_open", "after_close", "date_only"])
def test_existing_bounded_earnings_scope_contains_extra_d_minus_6(precision):
    chosen = event(time_precision=precision)
    start, end = plan_scope({"kind": "earnings", "events": [chosen]}, date(2024, 9, 1))
    required = session_window(date(2024, 6, 10), 6, 5)
    assert start <= required[0]
    assert end >= required[-1]
    assert (end - start).days < 150


def test_explicit_years_remain_bounded_when_current_fiscal_year_is_unconfirmed():
    result = compute(
        [event(), event(id="outside", fiscal_year=2022)],
        current_fiscal_year=None,
    )
    assert [row["key"] for row in result["date_observation"]["rows"]] == ["release-2023-q1"]
    assert date_row(result)["group"] == "observation"
    assert result["date_observation"]["effective_n"] == 0


def test_exact_clock_date_conflict_is_explicit_without_changing_precise_output():
    result = compute(
        [
            event(
                announced_at="2024-06-11T16:30:00-04:00",
                time_precision="exact",
                precise_time_supported=True,
            )
        ]
    )
    row = date_row(result)
    assert row["date_status"] == "conflicting"
    assert row["anchor"]["anchor_date"] is None
    assert not row["date_verified"]


def test_conflicting_report_period_end_sources_do_not_silently_choose_one():
    original = event()
    original["evidence"].append(
        {
            **original["evidence"][0],
            "period_end": "2024-04-01",
            "source_url": "https://example.com/other-observation",
        }
    )
    row = date_row(compute([original]))
    assert row["period_end"] is None
    assert len(row["sources"]) == 2


def test_period_kind_source_must_match_fiscal_identity_and_not_conflict():
    wrong = sourced_regular()
    wrong["evidence"][0]["fiscal_quarter"] = 2
    assert date_row(compute([wrong]))["period_kind"] == "unknown"
    assert compute([wrong])["date_observation"]["effective_n"] == 0
    conflicting = sourced_regular()
    conflicting["evidence"].append({
        **conflicting["evidence"][0], "source_url": "https://example.com/transition",
        "period_kind": "transition", "period_kind_evidence": "Synthetic source says transition period.",
    })
    assert date_row(compute([conflicting]))["period_kind"] == "unknown"
    assert compute([conflicting])["date_observation"]["effective_n"] == 0


def test_invalid_legacy_source_url_cannot_qualify_regular_period():
    for url in ("http://", "https://user@example.com/claim", "https://example.com:bad/claim"):
        item = sourced_regular()
        item["evidence"][0]["source_url"] = url
        result = compute([item])
        assert date_row(result)["period_kind"] == "unknown"
        assert result["date_observation"]["effective_n"] == 0
        assert date_row(result)["sources"] == []


def test_latest_manual_period_review_supersedes_old_source_without_leaking_support():
    record = sourced_regular()
    record["evidence"].extend([
        {
            "provider": "manual_review",
            "source_url": "https://example.com/synthetic-date-only",
            "announced_date": "2024-06-10",
            "fiscal_year": 2023, "fiscal_quarter": 1,
            "note": "Synthetic release date only",
        },
        {
            "provider": "manual_period_review",
            "source_url": "https://example.com/synthetic-quarterly-filing",
            "fiscal_year": 2023, "fiscal_quarter": 1,
            "period_kind": "regular",
            "period_kind_evidence": "Synthetic ordinary fiscal quarter cover",
        },
    ])
    row = date_row(compute([record]))
    assert row["period_kind"] == "regular"
    by_url = {source["url"]: source for source in row["sources"]}
    assert "period_kind" not in by_url["https://example.com/actual-release"]["supports"]
    assert "event_date" in by_url["https://example.com/synthetic-date-only"]["supports"]
    assert "period_kind" not in by_url["https://example.com/synthetic-date-only"]["supports"]
    assert "period_kind" in by_url["https://example.com/synthetic-quarterly-filing"]["supports"]
    assert "event_date" not in by_url["https://example.com/synthetic-quarterly-filing"]["supports"]
    record["evidence"].append({
        "provider": "manual_period_review", "period_kind": "unknown",
        "fiscal_year": 2023, "fiscal_quarter": 1,
    })
    row = date_row(compute([record]))
    assert row["period_kind"] == "unknown"
    assert all("period_kind" not in source["supports"] for source in row["sources"])
    assert not row["eligible"]


def test_old_period_source_without_explicit_fiscal_identity_cannot_raise_n():
    record = sourced_regular()
    source = dict(record["evidence"][0])
    source.pop("fiscal_year")
    source.pop("fiscal_quarter")
    record["evidence"] = [source]
    row = date_row(compute([record]))
    assert row["period_kind"] == "unknown"
    assert row["sources"][0]["supports"] == ["event_date"]
    assert not row["eligible"]
    assert compute([record])["date_observation"]["effective_n"] == 0


def test_legacy_combined_manual_source_cannot_attest_date_and_regular_period():
    record = sourced_regular()
    record["evidence"] = [{
        "provider": "manual_review", "source_url": "https://example.com/synthetic-10q-only",
        "announced_date": "2024-06-10", "fiscal_year": 2023, "fiscal_quarter": 1,
        "period_kind": "regular", "period_kind_evidence": "Synthetic ordinary-quarter claim",
    }]
    row = date_row(compute([record]))
    assert row["period_kind"] == "unknown"
    assert row["date_status"] == "unverified"
    assert row["anchor"]["original_date"] == "2024-06-10"  # Candidate retained, not an approved date.
    assert row["sources"][0]["supports"] == []
    assert "歧义" in row["sources"][0]["evidence_note"]
    assert compute([record])["date_observation"]["effective_n"] == 0


def test_legacy_combined_source_plus_separate_announcement_keeps_date_only():
    record = sourced_regular()
    record["evidence"] = [
        {
            "provider": "manual_review", "source_url": "https://example.com/synthetic-10q-only",
            "announced_date": "2024-06-10", "fiscal_year": 2023, "fiscal_quarter": 1,
            "period_kind": "regular", "period_kind_evidence": "Synthetic combined claim",
        },
        {
            "provider": "SEC release", "source_url": "https://example.com/synthetic-actual-release",
            "announced_date": "2024-06-10", "fiscal_year": 2023, "fiscal_quarter": 1,
        },
    ]
    row = date_row(compute([record]))
    assert row["date_status"] == "supported"
    assert row["period_kind"] == "unknown"
    by_url = {source["url"]: source["supports"] for source in row["sources"]}
    assert by_url["https://example.com/synthetic-10q-only"] == []
    assert by_url["https://example.com/synthetic-actual-release"] == ["event_date"]
    assert compute([record])["date_observation"]["effective_n"] == 0


def test_legacy_verified_flag_without_date_source_remains_visible_candidate():
    record = event(evidence=[])
    row = date_row(compute([record]))
    assert row["date_status"] == "unverified"
    assert row["anchor"]["original_date"] == "2024-06-10"
    assert not row["eligible"]
    assert compute([record])["date_observation"]["effective_n"] == 0


def test_unattributed_actual_time_does_not_enter_precise_history():
    supplied = sourced_regular(time_precision="exact", announced_at="2024-06-10T16:05:00-04:00",
                               precise_time_supported=True)
    # The release supports the date; its text does not support this manually typed minute.
    supplied["evidence"][0]["excerpt"] = "Synthetic source states the announcement date only."
    result = compute([supplied])
    row = date_row(result)
    assert row["date_verified"] and row["time_verified"] is False
    assert row["event_time"] is None
    assert result["effective_n"] == 0
    assert "event_time" not in row["sources"][0]["supports"]


def test_containing_sec_filing_gets_date_credit_not_undated_exhibit():
    supplied = sourced_regular()
    exhibit = supplied["evidence"][0]
    exhibit.pop("period_kind")
    exhibit.pop("period_kind_evidence")
    exhibit["announcement_context_source_url"] = "https://www.sec.gov/Archives/edgar/data/1/filing.txt"
    exhibit["announcement_date_evidence"] = [{"announced_date": "2024-06-10", "excerpt": "On June 10, 2024, issuer released results as Exhibit 99.1"}]
    supplied["evidence"].append({"provider": "SEC quarterly filing", "source_url": "https://www.sec.gov/Archives/edgar/data/1/10q.htm",
                                "fiscal_year": 2023, "fiscal_quarter": 1, "period_kind": "regular",
                                "period_kind_evidence": "Quarterly Report checked, transition unchecked"})
    row = date_row(compute([supplied]))
    supports = {source["url"]: source["supports"] for source in row["sources"]}
    assert "event_date" not in supports[exhibit["source_url"]]
    assert "event_date" in supports[exhibit["announcement_context_source_url"]]
    assert "event_date" not in supports["https://www.sec.gov/Archives/edgar/data/1/10q.htm"]
