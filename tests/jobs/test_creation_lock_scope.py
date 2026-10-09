"""Slow cache/response work must not retain shared command-creation locks."""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from iirp.analysis import requests
from iirp.db import session
from iirp.jobs import batches, planner
from iirp.jobs.handlers import execute_business
from iirp.jobs.operation_pool import OperationPool
from iirp.jobs.queue import claim
from iirp.models import (
    AnalysisRequest,
    AnalysisResult,
    Batch,
    BatchPlanSignal,
    RequestReceipt,
)
from sqlalchemy import func, select, text

from tests.analysis.test_performance_pipeline import params
from tests.analysis.test_shared_compute import RealRunner, plan, setup
from tests.jobs.test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401


def values(**overrides):
    return params(kind="monthly", current_year=2025, historical_years=1, **overrides)


@pytest.mark.parametrize("boundary", ["cache", "response"])
@pytest.mark.parametrize("relation", ["same_scope", "same_request", "same_security"])
def test_slow_local_work_does_not_hold_creation_locks(monkeypatch, boundary, relation):
    setup("monthly")
    first_values = values()
    following_values = {
        **first_values,
        **({} if relation == "same_request" else {"request_id": str(uuid.uuid4())}),
        **({"historical_years": 2} if relation == "same_security" else {}),
    }
    entered, release = threading.Event(), threading.Event()
    once = threading.Lock()
    blocked = False

    def gate(s, request):
        nonlocal blocked
        batch = s.get(Batch, request.batch_id)
        with once:
            selected = batch.request_id == first_values["request_id"] and not blocked
            if selected:
                blocked = True
        if selected:
            entered.set()
            assert release.wait(10), "test did not release the local-work boundary"

    if boundary == "response":
        original = requests.analysis_view

        def view(s, request, *args, **kwargs):
            gate(s, request)
            return original(s, request, *args, **kwargs)

        monkeypatch.setattr(requests, "analysis_view", view)
    else:
        original = planner._plan_compute

        def cache(s, scope, batch, security, *, cache_only=False):
            if cache_only:
                request = s.scalar(select(AnalysisRequest).where(AnalysisRequest.batch_id == batch.id))
                gate(s, request)
            return original(s, scope, batch, security, cache_only=cache_only)

        monkeypatch.setattr(planner, "_plan_compute", cache)
        monkeypatch.setattr(requests, "_plan_compute", cache)

    with session() as s:
        assert s.scalar(text("SHOW lock_timeout")) == "500ms"
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(requests.create_analysis, first_values)
        try:
            assert entered.wait(10)
            second = pool.submit(requests.create_analysis, following_values).result(timeout=10)
            assert not release.is_set(), "following command must finish before releasing local work"
        finally:
            release.set()
        first = first.result(timeout=10)
    assert (first["id"] == second["id"]) == (relation == "same_request")
    assert (first["batch_id"] == second["batch_id"]) == (relation == "same_request")
    with session() as s:
        expected = 1 if relation == "same_request" else 2
        assert s.scalar(select(func.count()).select_from(AnalysisRequest)) == expected
        assert s.scalar(select(func.count()).select_from(RequestReceipt)) == expected


def complete_cached_result():
    setup("monthly")
    first = requests.create_analysis(values())
    plan(first["id"])
    job = claim({"research_compute"})
    assert job is not None
    with closing(OperationPool()) as pool:
        execute_business(job, runner=RealRunner(pool))
    return requests.get_analysis(first["id"])


def test_first_response_still_includes_completed_cache():
    original = complete_cached_result()
    assert original["results"]
    created = requests.create_analysis(values())
    assert created["results"]
    assert created["id"] != original["id"]
    assert created["results"][0]["result_id"] != original["results"][0]["result_id"]
    assert created["results"][0]["data"]["periods"] == original["results"][0]["data"]["periods"]


@pytest.mark.parametrize("action", ["pause", "cancel"])
@pytest.mark.parametrize("boundary", ["before_cache", "after_cache"])
def test_control_around_cache_reuse_is_respected(monkeypatch, action, boundary):
    complete_cached_result()
    entered, release = threading.Event(), threading.Event()
    identifiers = []
    original = requests._reuse_analysis_cache

    def hold(identifier):
        identifiers.append(identifier)
        if boundary == "after_cache":
            original(identifier)
        entered.set()
        assert release.wait(10)
        if boundary == "before_cache":
            return original(identifier)

    monkeypatch.setattr(requests, "_reuse_analysis_cache", hold)
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(requests.create_analysis, values())
        try:
            assert entered.wait(10)
            with session() as s:
                request = s.get(AnalysisRequest, identifiers[0])
                batch_id = request.batch_id
                assert s.get(BatchPlanSignal, batch_id) is not None
            batches.control_batch(batch_id, action)
        finally:
            release.set()
        created = pending.result(timeout=10)
    assert created["status"] == {"pause": "PAUSED", "cancel": "CANCELLED"}[action]
    assert bool(created["results"]) == (boundary == "after_cache")
    with session() as s:
        assert s.scalar(select(func.count()).select_from(AnalysisResult)
                        .where(AnalysisResult.analysis_id == created["id"])) == int(boundary == "after_cache")
        batch = s.get(Batch, batch_id)
        assert batch.requested_action == action and batch.control_version == 1


def test_busy_batch_defers_cache_without_losing_durable_signal(monkeypatch):
    complete_cached_result()
    original = requests._reuse_analysis_cache

    def held_by_planner(identifier):
        with session() as holder, holder.begin():
            request = holder.get(AnalysisRequest, identifier)
            holder.get(Batch, request.batch_id, with_for_update=True)
            original(identifier)
            assert holder.get(BatchPlanSignal, request.batch_id) is not None

    monkeypatch.setattr(requests, "_reuse_analysis_cache", held_by_planner)
    created = requests.create_analysis(values())
    assert not created["results"]
    plan(created["id"])
    assert requests.get_analysis(created["id"])["results"]


def test_post_commit_failure_keeps_the_same_recoverable_request(monkeypatch):
    setup("monthly")
    submitted = values()

    def unavailable(_identifier):
        raise RuntimeError("synthetic failure after command commit")

    monkeypatch.setattr(requests, "_reuse_analysis_cache", unavailable)
    with pytest.raises(RuntimeError, match="after command commit"):
        requests.create_analysis(submitted)
    with session() as s:
        receipt = s.get(RequestReceipt, submitted["request_id"])
        assert receipt is not None
        request = s.scalar(select(AnalysisRequest).where(AnalysisRequest.batch_id == receipt.batch_id))
        identifier, batch_id = request.id, request.batch_id
        assert s.get(BatchPlanSignal, batch_id) is not None
    replay = requests.create_analysis(submitted)
    assert replay["id"] == identifier and replay["batch_id"] == batch_id
    with pytest.raises(ValueError, match="analysis.request_id_reused"):
        requests.create_analysis({**submitted, "historical_years": 2})
