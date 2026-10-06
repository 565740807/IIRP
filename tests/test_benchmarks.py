"""Disposable DB tests: version fences, shared demands, capacities and exports."""

import csv
import io
import json
import uuid
from datetime import date

from iirp import event_service, lifecycle
from iirp.benchmarks import benchmark_snapshot
from iirp.business_models import (
    AnalysisRequest,
    AnalysisResult,
    BatchJob,
    RequestScope,
    Security,
)
from iirp.business_worker import _persist, prepare_target
from iirp.contracts import AnalysisInput
from iirp.db import session
from iirp.market_data import resolve_metadata
from iirp.models import Job
from iirp.operations import operation
from iirp.queue import claim, fenced
from iirp.storage import save_object
from sqlalchemy import select
from test_event_service import analysis, plan, saved
from test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_database,
    refetch_prices,
    seed_prices,
    seed_security,
)


def baseline(symbol="^IXIC", prices=True):
    identifier = seed_security(symbol)
    with session() as s, s.begin():
        security = s.get(Security, identifier)
        security.instrument = "INDEX" if symbol.startswith("^") else "ETF"
    if prices:
        seed_prices(identifier, date(2022, 12, 1), date(2025, 1, 1), wide=True)
    return identifier


def research(**extra):
    return lifecycle.create_analysis(AnalysisInput(request_id=str(uuid.uuid4()), tickers=["AAPL"], kind="interval", current_year=2024,
        historical_years=1, start_mmdd="01-03", end_mmdd="01-10", benchmark="^IXIC", **extra).model_dump(mode="json"))


def publish(lease):
    payload = operation("research_compute", prepare_target(lease))
    source = save_object(json.dumps(payload).encode())
    assert fenced(lease, source=source, business_write=lambda s, j: _persist(s, j, payload, {}, source))
    return payload


def test_index_identity_contract_is_specific_to_composite_and_sp500():
    for symbol in ("^IXIC", "^GSPC", "^VIX"):
        identifier = seed_security(symbol)
        with session() as s, s.begin():
            security = s.get(Security, identifier)
            resolve_metadata(s, security, {"metadata": {"symbol": symbol, "quoteType": "INDEX", "currency": "USD", "exchange": "NIM"}})
            assert security.calendar == (None if symbol == "^VIX" else "XNYS")
            assert security.status == ("VERIFIED_MARKET" if symbol == "^VIX" else "VERIFIED")


def test_benchmark_refetch_rejects_old_worker_and_keeps_earlier_result_readable():
    stock = seed_security()
    seed_prices(stock, date(2022, 12, 1), date(2024, 1, 31), wide=True)
    other = baseline()
    created = research()
    lifecycle.plan_tick()
    first = claim({"research_compute"})
    publish(first)
    original = lifecycle.get_analysis(created["id"])["results"][0]
    assert original["is_current"] and original["expires_at"]
    assert claim({"research_compute"}) is None
    # A new benchmark fetch is a new input; the running computation is stale.
    b2 = refetch_prices(other, date(2024, 1, 4), date(2024, 1, 4), close=101)
    lifecycle.plan_tick()
    second = claim({"research_compute"})
    assert second.target["benchmark"]["dataset_id"] == b2
    prepared = operation("research_compute", prepare_target(second))
    b3 = refetch_prices(other, date(2024, 1, 5), date(2024, 1, 5), close=102)
    source = save_object(json.dumps(prepared).encode())
    assert fenced(second, source=source, business_write=lambda s, j: _persist(s, j, prepared, {}, source))
    with session() as s:
        assert not s.scalar(select(AnalysisResult.id).where(AnalysisResult.input_key == second.target["input_key"]))
    lifecycle.plan_tick()
    third = claim({"research_compute"})
    assert third.target["benchmark"]["dataset_id"] == b3
    publish(third)
    current = lifecycle.get_analysis(created["id"])
    assert current["results"][0]["input_version"] != original["input_version"]
    earlier = lifecycle.get_analysis(created["id"], original["result_id"])["results"][0]
    assert earlier["input_version"] == original["input_version"]
    exported = list(csv.DictReader(io.StringIO(lifecycle.export_analysis(created["id"], original["result_id"]).lstrip("\ufeff"))))
    assert {r["result_id"] for r in exported} == {original["result_id"]}
    assert "distribution" in {r["record_type"] for r in exported}
    assert json.loads(next(r for r in exported if r["record_type"] == "benchmark")["data"])["dataset_id"] == original["data"]["benchmark"]["dataset_id"]


def test_event_benchmark_download_is_shared_and_follows_controls():
    collection = saved()
    stock = collection["security_id"] if "security_id" in collection else None
    if not stock:
        with session() as s:
            stock = s.scalar(select(Security.id).where(Security.symbol == "AAPL"))
    seed_prices(stock, date(2022, 1, 1), date(2025, 1, 1))
    other = baseline(prices=False)
    first = analysis(collection, benchmark="^IXIC")
    assert plan(first["analysis_id"], 32)[0] == "RUNNING"
    second = analysis(collection, benchmark="^IXIC")
    plan(second["analysis_id"], 32)
    with session() as s:
        jobs = s.scalars(select(Job).where(Job.kind == "market_history", Job.target["security_id"].astext == other)).all()
        assert jobs
        assert all(len(s.scalars(select(BatchJob).where(BatchJob.job_id == j.id, BatchJob.active.is_(True))).all()) == 2 for j in jobs)
    lifecycle.control_batch(first["batch_id"], "pause")
    lease = claim({"market_history"})
    assert lease is not None
    lifecycle.control_batch(second["batch_id"], "cancel")
    assert not fenced(lease, status="SUCCEEDED")
    lifecycle.control_batch(first["batch_id"], "resume")
    lifecycle.plan_tick()
    assert claim({"market_history"}) is not None


def test_event_publish_captures_benchmark_once_and_empty_year_selection_stays_empty(monkeypatch):
    collection = saved()
    baseline()
    created = analysis(collection, benchmark="^IXIC")
    with session() as s:
        request = s.get(AnalysisRequest, created["analysis_id"])
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == request.batch_id))
        frozen = benchmark_snapshot(request.params, s.get(Security, scope.security_id), s)
    calls = []
    def snapshot(*args, **kwargs):
        calls.append(1)
        return frozen
    monkeypatch.setattr("iirp.event_pipeline.benchmark_snapshot", snapshot)
    with session() as s, s.begin():
        request = s.get(AnalysisRequest, created["analysis_id"])
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == request.batch_id))
        from iirp.event_pipeline import plan_event_compute
        row = plan_event_compute(s, request, scope, [32])
        assert row.inputs["benchmark"]["dataset_id"] == frozen["dataset_id"]
    assert len(calls) == 1
    empty = analysis(collection, years=[2024], excluded_years=[2024])
    result = event_service.get_analysis(empty["analysis_id"])
    assert result["params"]["years"] == [] and result["data"]["rows"] == []
