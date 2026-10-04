"""Server-owned earnings chronology and out-of-order source evidence; isolated DB only."""

from datetime import date, datetime, timezone
from types import SimpleNamespace

from iirp import earnings_data
from iirp.db import session
from iirp.earnings_data import correct_event, event_dict, persist_candidates
from test_earnings_lifecycle import (
    clean,  # noqa: F401
    event,
    isolated_database,  # noqa: F401
    release,
    save_release,
    setup,
    source,
)


def candidate(s, security, marker="candidate"):
    response = {"records": [{"announced_date": "2025-01-30", "announced_at": "2025-01-30T08:00:00-05:00"}], "complete": True, "marker": marker}
    job = SimpleNamespace(id=None, target={"security_id": security.id, "start_date": "2024-01-01", "end_date": "2025-12-31"}, checkpoint={})
    persist_candidates(s, job, response, source(s, response))
    s.flush()


def test_first_observation_survives_confirmation_and_late_provider_candidate(monkeypatch):
    from iirp.business_models import EarningsEvent
    from sqlalchemy import select

    first = datetime(2025, 1, 20, 12, tzinfo=timezone.utc)
    confirmed = datetime(2025, 1, 31, 12, tzinfo=timezone.utc)
    late = datetime(2025, 2, 1, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(earnings_data, "now", lambda: first)
    with session() as s, s.begin():
        security, _, _ = setup(s)
        candidate(s, security)
        item = s.scalar(select(EarningsEvent).where(EarningsEvent.security_id == security.id))
        assert item.is_estimate and not item.verified
        assert event_dict(item)["first_observed_at"] == first.isoformat()
        assert event_dict(item)["last_verified_at"] is None
        monkeypatch.setattr(earnings_data, "now", lambda: confirmed)
        save_release(s, security, release(), "actual release")
        assert item.verified and not item.is_estimate
        assert event_dict(item)["last_verified_at"] == confirmed.isoformat()
        monkeypatch.setattr(earnings_data, "now", lambda: late)
        candidate(s, security, "delayed candidate")
        view = event_dict(item)
        assert item.verified and not item.is_estimate
        assert view["first_observed_at"] == first.isoformat()
        assert view["last_verified_at"] == confirmed.isoformat()
        assert view["announced_at"] is None and not view["precise_time_supported"]
        assert view["updated_at"] == late.isoformat()


def test_legacy_event_first_observation_stays_unknown_after_new_evidence():
    with session() as s, s.begin():
        security, _, _ = setup(s)
        item = event(s, security)
        item.evidence = [{"provider": "yfinance", "source_hash": "legacy-observation-without-time"}]
        save_release(s, security, release(), "current check")
        assert event_dict(item)["first_observed_at"] is None
        assert event_dict(item)["last_verified_at"] is not None


def test_manual_review_records_new_publication_time_and_retains_prior_revision():
    with session() as s, s.begin():
        security, _, _ = setup(s)
        item = save_release(s, security, release(), "date evidence")
        reviewed = correct_event(s, item.id, {
            "revision": item.revision,
            "source_url": "https://example.com/synthetic-actual-publication",
            "note": "Synthetic source directly states the actual publication time",
            "announced_date": str(date(2025, 1, 30)),
            "announced_at": "2025-01-30T16:05:00-05:00",
            "time_precision": "exact", "time_evidence": "Synthetic issuer source says results first published at 4:05 p.m. ET", "fiscal_year": 2024, "fiscal_quarter": 4,
        })
        evidence = item.evidence[-1]
        assert evidence["announced_at"] == "2025-01-30T16:05:00-05:00"
        assert evidence["time_precision"] == "exact"
        assert evidence["previous"]["time_precision"] == "date_only"
        assert evidence["previous"]["announced_at"] is None
        assert reviewed["items"][0]["precise_time_supported"]


def test_separate_date_and_period_sources_version_and_revocation():
    from iirp.analytics.research import earnings_date_event

    with session() as s, s.begin():
        security, _, _ = setup(s)
        item = save_release(s, security, release(), "date evidence")
        initial_revision = item.revision
        values = {
            "revision": initial_revision,
            "source_url": "https://example.com/synthetic-announcement",
            "note": "Synthetic announcement says actual release date only",
            "announced_date": "2025-01-30", "time_precision": "date_only",
            "fiscal_year": 2024, "fiscal_quarter": 4,
            "period_kind": "regular",
            "period_kind_source_url": "https://example.com/synthetic-10q",
            "period_kind_evidence": "Synthetic 10-Q cover identifies ordinary quarter ended 2024-12-31",
        }
        correct_event(s, item.id, values)
        assert item.revision == initial_revision + 1
        reviewed = earnings_date_event(event_dict(item), date(2025, 2, 1))
        assert reviewed["period_kind"] == "regular"
        supports = {source["url"]: source["supports"] for source in reviewed["sources"]}
        assert "event_date" in supports[values["source_url"]]
        assert "period_kind" not in supports[values["source_url"]]
        assert "period_kind" in supports[values["period_kind_source_url"]]
        assert "event_date" not in supports[values["period_kind_source_url"]]
        prior = [source for source in reviewed["sources"] if source["url"] == values["period_kind_source_url"]][0]
        assert "Synthetic 10-Q" in prior["evidence_note"]

        legacy_date_only = {
            key: value for key, value in values.items()
            if key not in {"period_kind", "period_kind_source_url", "period_kind_evidence"}
        }
        correct_event(s, item.id, {**legacy_date_only, "revision": item.revision})
        assert earnings_date_event(event_dict(item), date(2025, 2, 1))["period_kind"] == "regular"
        correct_event(s, item.id, {
            **values, "revision": item.revision, "period_kind": "unknown",
            "period_kind_source_url": None, "period_kind_evidence": None,
            "note": "Synthetic recheck withdraws period classification",
        })
        withdrawn = earnings_date_event(event_dict(item), date(2025, 2, 1))
        assert item.revision == initial_revision + 3
        assert withdrawn["period_kind"] == "unknown"
        assert all("period_kind" not in source["supports"] for source in withdrawn["sources"])
        assert any(source.get("period_kind") == "regular" for source in item.evidence)
        assert item.evidence[-1]["period_kind"] == "unknown"


def test_manual_exact_time_requires_separate_source_passage():
    import pytest
    from iirp.contracts import EventCorrection
    from pydantic import ValidationError

    values = {
        "revision": 1, "fiscal_year": 2024, "fiscal_quarter": 4,
        "announced_date": "2025-01-30", "announced_at": "2025-01-30T16:05:00-05:00",
        "time_precision": "exact", "source_url": "https://example.com/synthetic-release",
        "note": "Synthetic date source only",
    }
    with pytest.raises(ValidationError, match="首次公开时间"):
        EventCorrection.model_validate(values)
    with session() as s, s.begin():
        security, _, _ = setup(s)
        item = save_release(s, security, release(), "date only")
        with pytest.raises(ValueError, match="首次公开时间"):
            correct_event(s, item.id, {**values, "revision": item.revision})
