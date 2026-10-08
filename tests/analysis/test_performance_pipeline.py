"""Performance regressions with synthetic data, only iirp_v1_test_* databases."""

import uuid
from datetime import date

from iirp.analysis import requests
from iirp.api.schemas import AnalysisInput
from iirp.db import session
from iirp.jobs import batches, planner
from iirp.jobs.queue import claim, fenced
from iirp.models import AnalysisResult, Job
from iirp.storage.objects import save_object
from sqlalchemy import func, select

from tests.jobs.test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    collection,
    lifecycle_database,
    refetch_prices,
    seed_prices,
    seed_security,
)


def params(**values):
    return AnalysisInput.model_validate(
        {
            "request_id": str(uuid.uuid4()),
            "kind": "monthly",
            "tickers": ["AAPL"],
            "historical_years": 1,
            "current_year": 2024,
            "benchmark": None,
            **values,
        }
    ).model_dump(mode="json")


def today_et():
    from iirp.analysis.calendar import ET
    from iirp.models import now

    return now().astimezone(ET).date()


def test_contiguous_eight_year_scope_uses_one_download_with_a_month_of_buffer():
    seed_security()
    batches.create_collection(collection(start_date="2017-12-29", end_date="2026-09-14"))
    planner.plan_tick()
    with session() as s:
        jobs = s.scalars(select(Job).where(Job.kind == "market_history")).all()
        assert len(jobs) == 1
        assert jobs[0].target["start_date"] == "2017-11-28"
        assert jobs[0].target["end_date"] == str(today_et())


def test_a_need_outside_the_cache_fetches_the_union_once():
    security = seed_security()
    seed_prices(security, date(2022, 12, 28), date(2022, 12, 29))
    batches.create_collection(collection(start_date="2022-12-28", end_date="2023-01-05"))
    planner.plan_tick()
    with session() as s:
        jobs = s.scalars(select(Job).where(Job.kind == "market_history")).all()
        assert [(j.target["start_date"], j.target["end_date"]) for j in jobs] == [
            ("2022-11-27", str(today_et()))
        ]


def test_refetch_replaces_the_not_started_compute_input():
    security = seed_security()
    first = seed_prices(security, date(2023, 1, 3), date(2023, 1, 5), wide=True)
    requests.create_analysis(params())
    planner.plan_tick()
    with session() as s:
        initial = s.scalar(select(Job).where(Job.kind == "research_compute"))
        initial_id = initial.id
        assert initial.target["dataset_id"] == first
    latest = refetch_prices(security, date(2023, 1, 6), date(2023, 1, 10))
    planner.plan_tick()
    with session() as s:
        queued = s.scalars(
            select(Job).where(Job.kind == "research_compute", Job.status == "QUEUED")
        ).all()
        assert len(queued) == 1
        assert queued[0].id == initial_id
        assert queued[0].target["dataset_id"] == latest


def finish_compute():
    from iirp.jobs.handlers import _persist, prepare_target
    from iirp.jobs.operations import operation

    job = claim({"research_compute"})
    assert job
    response = operation(job.kind, prepare_target(job))
    source = save_object(b"synthetic pipeline calculation evidence")
    assert fenced(
        job, source=source, business_write=lambda s, j: _persist(s, j, response, {}, source)
    )
    planner.plan_tick()


def test_identical_valid_inputs_reuse_result_without_new_compute_or_download():
    security = seed_security()
    seed_prices(security, date(2022, 12, 1), date(2024, 12, 31), wide=True)
    original = requests.create_analysis(params())
    planner.plan_tick()
    finish_compute()
    with session() as s:
        before = s.scalar(select(func.count()).select_from(Job))
    reused = requests.create_analysis(params())
    assert reused["results"], "already valid result must be included in command response"
    planner.plan_tick()
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == before
        assert (
            s.scalar(
                select(func.count())
                .select_from(AnalysisResult)
                .where(AnalysisResult.analysis_id == reused["id"])
            )
            == 1
        )
    assert (
        reused["results"][0]["data"]["periods"]
        == requests.get_analysis(original["id"])["results"][0]["data"]["periods"]
    )


def test_execution_precheck_skips_obsolete_input_before_preparation(monkeypatch):
    from iirp.jobs import handlers

    security = seed_security()
    seed_prices(security, date(2023, 1, 3), date(2023, 1, 5), wide=True)
    requests.create_analysis(params())
    planner.plan_tick()
    job = claim({"research_compute"})
    assert job
    refetch_prices(security, date(2023, 1, 6), date(2023, 1, 10))

    def forbidden(_):
        raise AssertionError("obsolete computation must never load bars or start a child")

    monkeypatch.setattr(handlers, "prepare_target", forbidden)
    handlers.execute_business(job)
    with session() as s:
        assert s.get(Job, job.id).result["skipped_before_compute"] is True


def test_changed_research_conditions_do_not_reuse_previous_result():
    security = seed_security()
    seed_prices(security, date(2022, 12, 1), date(2024, 12, 31), wide=True)
    requests.create_analysis(params())
    planner.plan_tick()
    finish_compute()
    other = requests.create_analysis(params(historical_years=2))
    assert not other["results"]
    planner.plan_tick()
    with session() as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.kind == "research_compute", Job.status == "QUEUED")
            )
            == 1
        )


def test_two_demands_for_the_same_prices_share_one_fetch():
    from iirp.models import BatchJob, RequestScope

    seed_security()
    first = batches.create_collection(collection(start_date="2020-01-01", end_date="2024-12-31"))
    second = batches.create_collection(collection(start_date="2020-01-01", end_date="2024-12-31"))
    planner.plan_tick()
    with session() as s:
        jobs = s.scalars(select(Job).where(Job.kind == "market_history")).all()
        assert len(jobs) == 1
        for batch in (first, second):
            linked = set(
                s.scalars(
                    select(BatchJob.job_id)
                    .join(RequestScope, RequestScope.id == BatchJob.scope_id)
                    .where(RequestScope.batch_id == batch["batch_id"])
                )
            )
            assert linked == {jobs[0].id}


def test_warm_child_reuse_crash_recovery_deadline_and_control():
    import os
    import signal

    import pytest
    from iirp.jobs.operation_pool import OperationChild, OperationInterrupted

    child = OperationChild()
    try:
        target = {"params": params(), "bars": []}
        assert child.run("research_compute", target)["ok"]
        pid = child.proc.pid
        assert child.run("research_compute", target)["ok"]
        assert child.proc.pid == pid
        os.killpg(pid, signal.SIGKILL)
        child.proc.wait(timeout=5)
        assert child.run("research_compute", target)["ok"]
        assert child.proc.pid != pid
        with pytest.raises(OperationInterrupted):
            child.run("research_compute", target, lambda: False)
        assert child.proc is None
        with pytest.raises(TimeoutError):
            child.run("research_compute", target, deadline=0.001)
        assert child.proc is None
        assert child.run("research_compute", target)["ok"]
    finally:
        child.close()


def test_http_timing_counts_actual_calls_and_keeps_errors_without_secrets():
    from types import SimpleNamespace

    import pytest
    from iirp.market.http import HttpTiming

    calls = []

    def request(method, url, **kwargs):
        calls.append((method, kwargs))
        if len(calls) == 2:
            raise TimeoutError("synthetic network deadline")
        return SimpleNamespace(status_code=200)

    timing = HttpTiming(SimpleNamespace(request=request))
    timing.session.request("GET", "https://query1.finance.yahoo.com/chart?crumb=secret", timeout=12)
    with pytest.raises(TimeoutError):
        timing.session.request("GET", "https://query2.finance.yahoo.com/chart?token=private")
    measured = timing.snapshot()
    assert measured["http_requests"] == 2
    assert measured["http_seconds"] >= 0
    assert measured["http_observations"][1]["status"] is None
    assert "secret" not in str(measured) and "private" not in str(measured)
    assert calls[0][1]["timeout"] == 12
    timing.reset()
    assert timing.snapshot()["http_requests"] == 0


def test_calendar_change_does_not_reuse_legacy_or_current_result():
    from iirp.models import Security

    security = seed_security()
    seed_prices(security, date(2022, 12, 1), date(2024, 12, 31), wide=True)
    requests.create_analysis(params())
    planner.plan_tick()
    finish_compute()
    with session() as s, s.begin():
        s.get(Security, security).calendar = "XNAS"
    assert not requests.create_analysis(params())["results"]


def test_results_publish_only_for_the_current_stock_and_benchmark_caches():
    from iirp.analysis.benchmarks import benchmark_snapshot
    from iirp.analysis.pipeline import safe_publication
    from iirp.models import AnalysisRequest, PriceCache, Security

    stock_id = seed_security()
    stock_data = seed_prices(stock_id, date(2023, 1, 3), date(2023, 1, 5), wide=True)
    view = requests.create_analysis(params(benchmark="^GSPC"))

    def target_now():
        with session() as s:
            request, stock, data = s.get(AnalysisRequest, view["id"]), s.get(Security, stock_id), s.get(PriceCache, stock_data)
            snapshot = benchmark_snapshot(request.params, stock, s)
            return {"dataset_id": stock_data, "benchmark": snapshot,
                    "input_key": planner.research_input_key(request, data, stock, snapshot)}

    def publishable(target):
        with session() as s:
            request, stock, data = s.get(AnalysisRequest, view["id"]), s.get(Security, stock_id), s.get(PriceCache, stock_data)
            return safe_publication(s, request, stock, target, data)

    stock_only = target_now()
    assert publishable(stock_only)
    benchmark_id = seed_security("^GSPC")
    with session() as s, s.begin():
        s.get(Security, benchmark_id).instrument = "INDEX"
    seed_prices(benchmark_id, date(2023, 1, 3), date(2023, 1, 5), wide=True)
    paired = target_now()
    assert paired["benchmark"]["status"] == "available"
    # The benchmark arrived: a stock-only computation is recomputed with it.
    assert not publishable(stock_only) and publishable(paired)
    refetch_prices(benchmark_id, date(2023, 1, 6), date(2023, 1, 10))
    assert not publishable(paired)


def test_input_facts_and_calculation_version_invalidate_cache(monkeypatch):
    from iirp.analysis import research

    security = seed_security()
    seed_prices(security, date(2022, 12, 1), date(2024, 12, 31))
    requests.create_analysis(params())
    planner.plan_tick()
    finish_compute()
    monkeypatch.setattr(research, "CALCULATION_VERSION", "synthetic-new-calculation-version")
    assert not requests.create_analysis(params())["results"]


def test_shared_quote_demand_stays_attached_after_completion():
    from iirp.models import Batch, BatchJob, RequestScope

    security = seed_security()
    with session() as s, s.begin():
        scopes = []
        for trigger in ("automatic", "manual"):
            batch = Batch(request_id=str(uuid.uuid4()), scope_key=str(uuid.uuid4()), kind="market_quotes", title="synthetic quote", params={}, trigger=trigger)
            s.add(batch)
            s.flush()
            scope = RequestScope(batch_id=batch.id, symbol="AAPL", security_id=security, start_date=date(2024, 1, 1), end_date=date(2024, 1, 5), checkpoint={})
            s.add(scope)
            s.flush()
            planner._plan_market(s, scope, batch, [32])
            scopes.append((scope, batch))
        jobs = s.scalars(select(Job).where(Job.kind == "market_quote")).all()
        assert len(jobs) == 1
        assert len(s.scalars(select(BatchJob).where(BatchJob.job_id == jobs[0].id)).all()) == 2
        jobs[0].status = "SUCCEEDED"
        s.flush()
        for scope, batch in scopes:
            planner._plan_market(s, scope, batch, [32])
            assert scope.status == "READY"
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "market_quote")) == 1


class CountingYahoo:
    """Stands in for the provider subprocess and records every Yahoo request."""

    def __init__(self):
        self.calls = []

    def run(self, kind, target, checkpoint):
        from iirp.analysis.calendar import last_completed_session, sessions

        self.calls.append((kind, target["symbol"], target["start_date"], target["end_date"]))
        through = min(date.fromisoformat(target["end_date"]), last_completed_session())
        days = sessions(date.fromisoformat(target["start_date"]), through)
        records = [{"date": str(day), **dict.fromkeys(("open", "high", "low", "close", "adj_close"), "100"),
                    "volume": "10", "splits": "0", "dividends": "0"} for day in days]
        return {"ok": True, "data": {"provider": "synthetic", "library_version": "fixture",
                "fetched_at": "2024-01-01T00:00:00+00:00", "records": records, "metadata": {}}}
