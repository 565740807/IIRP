"""Independent review regressions. All DB state is iirp_v1_test_* synthetic data.

Do not run concurrently with capacity/restore acceptance; coordinator schedules it.
"""
from datetime import date, datetime, timezone

import pytest
from iirp import models
from iirp.analysis import requests
from iirp.analysis.dependencies import research_ranges
from iirp.analysis.pipeline import frozen_coverage
from iirp.analysis.research import compute_research
from iirp.db import session
from iirp.jobs import planner
from iirp.market.cache import price_bars
from iirp.models import AnalysisRequest, AnalysisResult, Batch, PriceCache, Security
from sqlalchemy import func, select

from tests.analysis.test_performance_pipeline import params
from tests.analysis.test_research import history
from tests.clock import set_clock
from tests.jobs.test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_database,
    seed_prices,
    seed_security,
)


def _current_but_incomplete(monkeypatch):
    stamp = datetime(2024, 1, 5, 23, tzinfo=timezone.utc)
    set_clock(monkeypatch, lambda: stamp)
    security_id = seed_security()
    dataset_id = seed_prices(security_id, date(2023, 1, 3), date(2023, 1, 4))
    view = requests.create_analysis(params(kind="interval", start_mmdd="01-03", end_mmdd="01-05"))
    with session() as s, s.begin():
        request = s.get(AnalysisRequest, view["id"])
        security = s.get(Security, security_id)
        dataset = s.get(PriceCache, dataset_id)
        key = planner.research_input_key(request, dataset, security)
        bars, _ = price_bars(s, security_id, dataset_id)
        result = AnalysisResult(analysis_id=request.id, security_id=security_id, input_key=key,
            inputs={"dataset_id": dataset_id, "params": request.params,
                    "coverage": frozen_coverage(s, request, security, dataset)},
            data=compute_research(request.params, bars, today=date(2024, 1, 5)),
            expires_at=dataset.expires_at)
        s.add(result)
        s.get(Batch, request.batch_id).status = "PARTIAL"
    result = requests.get_analysis(view["id"])
    assert result["results"][0]["is_current"]
    assert result["results"][0]["coverage"]["complete"] is False
    return result


def test_explicit_latest_refresh_retries_terminal_same_input_price_gaps(monkeypatch):
    """Current fingerprint means correct available inputs, not complete coverage."""
    previous = _current_but_incomplete(monkeypatch)
    updated = requests.refresh_analysis(previous["id"], force=True)
    assert updated["id"] != previous["id"]
    assert updated["batch"]["status"] in models.ACTIVE
    frozen = requests.get_analysis(previous["id"], previous["results"][0]["result_id"])
    assert frozen["results"][0]["data"] == previous["results"][0]["data"]
    # Rapid manual retries share the still-active refresh, instead of multiplying work.
    repeated = requests.refresh_analysis(previous["id"], force=True)
    assert repeated["id"] == updated["id"]


def test_automatic_refresh_does_not_loop_on_an_unexpired_gap(monkeypatch):
    original = _current_but_incomplete(monkeypatch)
    with session() as s:
        before = s.scalar(select(func.count()).select_from(AnalysisRequest))
    for _ in range(3):
        assert requests.refresh_analysis(original["id"])["id"] == original["id"]
    with session() as s:
        assert s.scalar(select(func.count()).select_from(AnalysisRequest)) == before


@pytest.mark.parametrize("kind", ["monthly", "interval"])
def test_range_bounded_loading_keeps_full_financial_output_identical(kind):
    cutoff = date(2024, 6, 30)
    values = {"kind": kind, "cutoff_date": str(cutoff), "years": [2022, 2023], "current_year": 2024,
              "month": 2, "comparison": "complete",
              "start_mmdd": "12-15", "end_mmdd": "01-20", "cross_year": True}
    bars = history(date(2020, 12, 1), date(2024, 12, 31))
    ranges = research_ranges(values, "XNYS")
    clipped = [row for row in bars if any(str(first) <= row["date"] <= str(last) for first, last in ranges)]
    benchmark = {"symbol": "SYNTHETIC", "status": "available", "bars": bars}
    full = compute_research(values, bars, today=cutoff, benchmark=benchmark)
    trimmed = compute_research(values, clipped, today=cutoff, benchmark={**benchmark, "bars": clipped})
    assert trimmed == full


def test_force_gap_refresh_retries_source_work_instead_of_only_wrapping_failed_job(monkeypatch):
    from iirp.models import BatchJob, Job, RequestScope

    previous = _current_but_incomplete(monkeypatch)
    with session() as s, s.begin():
        batch = s.get(Batch, previous["batch_id"])
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch.id))
        planner._plan_market(s, scope, batch, [10])
        jobs = s.scalars(select(Job).join(BatchJob, BatchJob.job_id == Job.id)
            .where(BatchJob.scope_id == scope.id, Job.kind == "market_history")).all()
        assert jobs, "fixture must produce real planned price gaps"
        failed_ids = {job.id for job in jobs}
        for job in jobs:
            job.status, job.error = "FAILED", "Synthetic external failure, isolated test only"
        batch.status, scope.status = "PARTIAL", "PARTIAL"
    updated = requests.refresh_analysis(previous["id"], force=True)
    planner.plan_tick()
    with session() as s:
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == updated["batch_id"]))
        jobs = s.scalars(select(Job).join(BatchJob, BatchJob.job_id == Job.id)
            .where(BatchJob.scope_id == scope.id, Job.kind == "market_history")).all()
        assert any(job.id not in failed_ids and job.status in models.ACTIVE for job in jobs)
        assert all(s.get(Job, identifier).status == "FAILED" for identifier in failed_ids), "retry preserves old task evidence"
