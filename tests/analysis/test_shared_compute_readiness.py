"""Shared numerical results do not complete an unresolved benchmark demand."""

from datetime import date, datetime, timezone

import pytest
from iirp.analysis import requests
from iirp.db import session
from iirp.jobs import batches, planner
from iirp.jobs.queue import claim, fenced
from iirp.models import AnalysisResult, Batch, Job, RequestScope, Security
from sqlalchemy import select

from tests.analysis.test_performance_pipeline import finish_compute, params
from tests.analysis.test_shared_compute import setup
from tests.clock import set_clock
from tests.jobs.test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_database,
    seed_prices,
    seed_security,
)
from tests.zh import zh


@pytest.mark.parametrize("blocker", ["identity_mismatch", "failed_history"])
def test_shared_stock_result_preserves_benchmark_failure_and_retry(blocker):
    create = setup("monthly")
    benchmark_id = seed_security("BENCH")
    with session() as s, s.begin():
        benchmark = s.get(Security, benchmark_id)
        benchmark.instrument = "ETF"
        if blocker == "identity_mismatch":
            benchmark.currency = "CAD"
    requests = [create(benchmark="BENCH"), create(benchmark="BENCH")]
    planner.plan_tick()
    if blocker == "failed_history":
        download = claim({"market_history"})
        assert download is not None
        assert download.target["security_id"] == benchmark_id
        assert fenced(download, status="FAILED", error="synthetic benchmark failure")
        assert claim({"market_history"}) is None
    finish_compute()
    planner.plan_tick()

    with session() as s:
        computations = s.scalars(select(Job).where(Job.kind == "research_compute")).all()
        assert len(computations) == 1 and computations[0].status == "SUCCEEDED"
        published = s.scalars(select(AnalysisResult)).all()
        assert len(published) == 2
        assert {row.analysis_id for row in published} == {item[0] for item in requests}
        frozen_ids = {row.id for row in published}
        for _, batch_id in requests:
            batch = s.get(Batch, batch_id)
            scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch_id))
            assert batch.status == "PARTIAL", "Stock-only output cannot complete the benchmark demand"
            assert scope.status == "PARTIAL"
            expected = (
                "证券类型、币种或交易日历" if blocker == "identity_mismatch"
                else "synthetic benchmark failure"
            )
            assert expected in zh(scope.wait_reason or "")

    # Repair only synthetic local facts, then exercise the normal explicit
    # retry path. Never execute a provider to satisfy this test's benchmark.
    with session() as s, s.begin():
        s.get(Security, benchmark_id).currency = "USD"
    seed_prices(benchmark_id, date(2022, 1, 1), date(2027, 1, 1))
    for _, batch_id in requests:
        batches.control_batch(batch_id, "retry_failed")
    if blocker == "failed_history":
        retried = claim({"market_history"})
        assert retried is not None and retried.id == download.id
        assert fenced(retried, status="SUCCEEDED")
    planner.plan_tick()
    finish_compute()
    planner.plan_tick()
    with session() as s:
        published = s.scalars(select(AnalysisResult)).all()
        assert len(published) == 4
        assert frozen_ids <= {row.id for row in published}
        for _, batch_id in requests:
            scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch_id))
            assert s.get(Batch, batch_id).status == "SUCCEEDED"
            assert scope.status == "READY" and scope.wait_reason is None


def test_completed_stock_result_preserves_a_persisted_future_scope(monkeypatch):
    completed = date(2024, 1, 5)
    monkeypatch.setattr(
        "iirp.analysis.calendar.last_completed_session", lambda *args, **kwargs: completed
    )
    set_clock(monkeypatch, lambda: datetime(2024, 1, 5, 22, tzinfo=timezone.utc))
    security_id = seed_security()
    seed_prices(security_id, date(2022, 12, 30), completed)
    created = requests.create_analysis(params())
    with session() as s, s.begin():
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == created["batch_id"]))
        # Reproduce a persisted collection range extending beyond the frozen
        # research cutoff; do not manufacture a COMPLETE coverage response.
        scope.end_date = date(2024, 1, 10)
    planner.plan_tick()
    finish_compute()
    with session() as s:
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == created["batch_id"]))
        # Sessions that have not closed are not fetched; the result is ready.
        assert scope.status == "READY"
        assert s.get(Batch, created["batch_id"]).status == "SUCCEEDED"
        assert s.scalar(select(AnalysisResult.id).where(AnalysisResult.analysis_id == created["id"]))
        assert s.scalar(select(Job.id).where(Job.kind == "market_history")) is None
        computations = s.scalars(select(Job).where(Job.kind == "research_compute")).all()
        assert len(computations) == 1 and computations[0].status == "SUCCEEDED"
