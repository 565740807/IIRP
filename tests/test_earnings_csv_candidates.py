"""Raw CSV claims never verify events or overwrite reviewed native facts."""

from datetime import date

from iirp.business_models import EarningsEvent
from iirp.db import session
from iirp.earnings_data import correct_event, event_dict
from iirp.imports import preview
from sqlalchemy import select
from test_imports_lifecycle import complete, security, values
from test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401

CSV = "announced_date,fiscal_year,fiscal_quarter,time_precision,announced_at\n2024-01-03,2024,1,exact,2024-01-03T16:05:00-05:00\n"


def test_csv_confirmation_only_saves_candidate_until_separate_fact_review():
    security()
    item = preview(values(CSV, kind="earnings"))
    complete(item)
    with session() as s, s.begin():
        event = s.scalar(select(EarningsEvent))
        assert not event.verified
        assert not event_dict(event)["precise_time_supported"]
        assert event.status == "CANDIDATE"
        assert event.announced_at is None and event.time_precision == "date_only"
        assert event.evidence[-1]["candidate"]["announced_at"] == "2024-01-03T16:05:00-05:00"
        confirmed = correct_event(s, event.id, {
            "revision": event.revision, "source_url": "https://example.test/direct-actual-release",
            "note": "Synthetic explicit source review, separate from CSV admission",
            "announced_date": "2024-01-03", "announced_at": "2024-01-03T16:05:00-05:00",
            "time_precision": "exact", "time_evidence": "Synthetic issuer source says actual release at 4:05 p.m. ET", "fiscal_year": 2024, "fiscal_quarter": 1,
        })
        assert confirmed["items"][0]["precise_time_supported"]


def test_csv_cannot_overwrite_existing_verified_fiscal_period_or_release_time():
    identity = security()
    with session() as s, s.begin():
        original = EarningsEvent(security_id=identity, announced_date=date(2024, 1, 3),
            announced_at=None, time_precision="after_close", fiscal_year=2023, fiscal_quarter=4,
            verified=True, is_estimate=False, status="VERIFIED", revision=1,
            evidence=[{"provider": "manual_review", "source_url": "https://example.test/original"}])
        s.add(original)
    complete(preview(values(CSV, kind="earnings")))
    with session() as s:
        event = s.scalar(select(EarningsEvent))
        assert (event.fiscal_year, event.fiscal_quarter) == (2023, 4)
        assert event.verified and event.time_precision == "after_close" and event.announced_at is None
        assert event.evidence[0]["source_url"] == "https://example.test/original"
        assert event.evidence[-1]["candidate"]["fiscal_year"] == 2024
        assert event.evidence[-1]["candidate"]["fiscal_quarter"] == 1


def test_duplicate_csv_keeps_one_candidate_observation_and_preview_does_not_read_prices(monkeypatch):
    from iirp import imports

    security()
    def no_price_scan(*args, **kwargs):
        raise AssertionError("Earnings CSV preview must not scan the stock price dataset")
    monkeypatch.setattr(imports, "price_bars", no_price_scan)
    first = preview(values(CSV, kind="earnings"))
    complete(first)
    with session() as s:
        original = s.scalar(select(EarningsEvent))
        revision = original.revision
        evidence = original.evidence
    complete(preview(values(CSV, kind="earnings")))
    with session() as s:
        current = s.scalar(select(EarningsEvent))
        assert current.revision == revision and current.evidence == evidence
        assert not current.verified
