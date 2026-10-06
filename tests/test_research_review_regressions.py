"""Independent review regressions. All DB state is iirp_v1_test_* synthetic data.

Do not run concurrently with capacity/restore acceptance; coordinator schedules it.
"""
from datetime import date, datetime, timezone

import pytest
from iirp import lifecycle
from iirp.analytics.research import compute_research
from iirp.business_models import AnalysisRequest, AnalysisResult, Batch, PriceCache, Security
from iirp.db import session
from iirp.price_cache import price_bars
from iirp.research_dependencies import research_ranges
from iirp.research_pipeline import frozen_coverage
from sqlalchemy import func, select
from test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_database,
    seed_prices,
    seed_security,
)
from test_performance_pipeline import params
from test_research import history


def _current_but_incomplete(monkeypatch):
    stamp = datetime(2024, 1, 5, 23, tzinfo=timezone.utc)
    monkeypatch.setattr(lifecycle, "now", lambda: stamp)
    security_id = seed_security()
    dataset_id = seed_prices(security_id, date(2023, 1, 3), date(2023, 1, 4))
    view = lifecycle.create_analysis(params(kind="interval", start_mmdd="01-03", end_mmdd="01-05"))
    with session() as s, s.begin():
        request = s.get(AnalysisRequest, view["id"])
        security = s.get(Security, security_id)
        dataset = s.get(PriceCache, dataset_id)
        key = lifecycle.research_input_key(request, dataset, [], security)
        bars, _ = price_bars(s, security_id, dataset_id)
        result = AnalysisResult(analysis_id=request.id, security_id=security_id, input_key=key,
            inputs={"dataset_id": dataset_id, "params": request.params,
                    "coverage": frozen_coverage(s, request, security, dataset)},
            data=compute_research(request.params, bars, today=date(2024, 1, 5)),
            expires_at=dataset.expires_at)
        s.add(result)
        s.get(Batch, request.batch_id).status = "PARTIAL"
    result = lifecycle.get_analysis(view["id"])
    assert result["results"][0]["is_current"]
    assert result["results"][0]["coverage"]["complete"] is False
    return result


def test_explicit_latest_refresh_retries_terminal_same_input_price_gaps(monkeypatch):
    """Current fingerprint means correct available inputs, not complete coverage."""
    previous = _current_but_incomplete(monkeypatch)
    updated = lifecycle.refresh_analysis(previous["id"], force=True)
    assert updated["id"] != previous["id"]
    assert updated["batch"]["status"] in lifecycle.ACTIVE
    frozen = lifecycle.get_analysis(previous["id"], previous["results"][0]["result_id"])
    assert frozen["results"][0]["data"] == previous["results"][0]["data"]
    # Rapid manual retries share the still-active refresh, instead of multiplying work.
    repeated = lifecycle.refresh_analysis(previous["id"], force=True)
    assert repeated["id"] == updated["id"]


def test_automatic_refresh_does_not_loop_on_an_unexpired_gap(monkeypatch):
    original = _current_but_incomplete(monkeypatch)
    with session() as s:
        before = s.scalar(select(func.count()).select_from(AnalysisRequest))
    for _ in range(3):
        assert lifecycle.refresh_analysis(original["id"])["id"] == original["id"]
    with session() as s:
        assert s.scalar(select(func.count()).select_from(AnalysisRequest)) == before


@pytest.mark.parametrize("kind", ["monthly", "interval", "earnings"])
def test_range_bounded_loading_keeps_full_financial_output_identical(kind):
    cutoff = date(2024, 6, 30)
    values = {"kind": kind, "cutoff_date": str(cutoff), "years": [2022, 2023], "current_year": 2024,
              "current_fiscal_year": 2024, "month": 2, "comparison": "complete",
              "start_mmdd": "12-15", "end_mmdd": "01-20", "cross_year": True, "quarter": 1, "window": 5}
    events = [{"id": f"synthetic-{year}-{quarter}", "fiscal_year": year, "fiscal_quarter": quarter,
               "announced_date": f"{year}-{month:02d}-15", "announced_at": f"{year}-{month:02d}-15T08:00:00-05:00",
               "time_precision": "exact", "verified": True}
              for year in (2021, 2022, 2023, 2024) for quarter, month in ((1, 2), (2, 5), (3, 8), (4, 11))]
    bars = history(date(2020, 12, 1), date(2024, 12, 31))
    ranges = research_ranges(values, "XNYS", events)
    clipped = [row for row in bars if any(str(first) <= row["date"] <= str(last) for first, last in ranges)]
    benchmark = {"symbol": "SYNTHETIC", "status": "available", "bars": bars}
    full = compute_research(values, bars, events, today=cutoff, benchmark=benchmark)
    trimmed = compute_research(values, clipped, events, today=cutoff, benchmark={**benchmark, "bars": clipped})
    assert trimmed == full


def test_force_gap_refresh_retries_source_work_instead_of_only_wrapping_failed_job(monkeypatch):
    from iirp.business_models import BatchJob, RequestScope
    from iirp.models import Job

    previous = _current_but_incomplete(monkeypatch)
    with session() as s, s.begin():
        batch = s.get(Batch, previous["batch_id"])
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch.id))
        lifecycle._plan_market(s, scope, batch, [10])
        jobs = s.scalars(select(Job).join(BatchJob, BatchJob.job_id == Job.id)
            .where(BatchJob.scope_id == scope.id, Job.kind == "market_history")).all()
        assert jobs, "fixture must produce real planned price gaps"
        failed_ids = {job.id for job in jobs}
        for job in jobs:
            job.status, job.error = "FAILED", "Synthetic external failure, isolated test only"
        batch.status, scope.status = "PARTIAL", "PARTIAL"
    updated = lifecycle.refresh_analysis(previous["id"], force=True)
    lifecycle.plan_tick()
    with session() as s:
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == updated["batch_id"]))
        jobs = s.scalars(select(Job).join(BatchJob, BatchJob.job_id == Job.id)
            .where(BatchJob.scope_id == scope.id, Job.kind == "market_history")).all()
        assert any(job.id not in failed_ids and job.status in lifecycle.ACTIVE for job in jobs)
        assert all(s.get(Job, identifier).status == "FAILED" for identifier in failed_ids), "retry preserves old task evidence"
