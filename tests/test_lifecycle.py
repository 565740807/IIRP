"""Independent A-stage acceptance against disposable PostgreSQL databases.

All seeded identities, prices and evidence are synthetic. No provider executes;
these tests do not assert live data completeness or complete V1 acceptance.
"""

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from iirp import lifecycle
from iirp.analytics.calendar import last_completed_session, sessions
from iirp.business_models import (
    AnalysisRequest,
    Batch,
    BatchJob,
    CollectionStrategy,
    JobDependency,
    PriceCache,
    PriceCacheBar,
    RequestScope,
    Security,
    SourceObservation,
)
from iirp.config import ROOT, settings
from iirp.contracts import AnalysisInput, CollectionInput
from iirp.db import engine, session
from iirp.models import Base, Coverage, Job, SourceObject, now
from iirp.queue import claim, ensure_defaults, fenced, recover
from iirp.storage import save_object
from psycopg import sql
from sqlalchemy import func, insert, select, text
from sqlalchemy.engine import make_url


@pytest.fixture(scope="module", autouse=True)
def lifecycle_database():
    """Unique module database; never shares foundation tests or live tables."""
    url = make_url(settings().database_url)
    database = "iirp_v1_test_lifecycle_" + uuid.uuid4().hex[:10]
    admin = psycopg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        dbname="postgres",
        autocommit=True,
    )
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    original = os.environ.get("IIRP_DATABASE_URL")
    os.environ["IIRP_DATABASE_URL"] = url.set(database=database).render_as_string(
        hide_password=False
    )
    engine.cache_clear()
    settings.cache_clear()
    try:
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        yield
    finally:
        engine().dispose()
        engine.cache_clear()
        if original is None:
            os.environ.pop("IIRP_DATABASE_URL", None)
        else:
            os.environ["IIRP_DATABASE_URL"] = original
        settings.cache_clear()
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
        admin.close()


@pytest.fixture(autouse=True)
def clean_lifecycle(tmp_path):
    with engine().begin() as conn:
        conn.execute(
            text(
                "TRUNCATE "
                + ",".join('"' + table.name + '"' for table in Base.metadata.sorted_tables)
                + " CASCADE"
            )
        )
    ensure_defaults()
    old_runtime = settings().runtime_dir
    settings().runtime_dir = tmp_path
    yield
    settings().runtime_dir = old_runtime


@pytest.fixture
def lifecycle_client():
    from iirp.api import app

    with TestClient(app, headers={"X-IIRP-Client": "web"}) as client:
        yield client


def collection(**overrides):
    return CollectionInput.model_validate(
        {
            "request_id": str(uuid.uuid4()),
            "kind": "market_history",
            "tickers": ["AAPL"],
            "start_date": "2023-01-03",
            "end_date": "2023-01-10",
            **overrides,
        }
    ).model_dump(mode="json")


def seed_security(symbol="AAPL"):
    with session() as s, s.begin():
        row = s.scalar(select(Security).where(Security.symbol == symbol))
        if row is None:
            row = Security(symbol=symbol)
            s.add(row)
        row.name, row.instrument, row.currency = "Synthetic test security", "EQUITY", "USD"
        row.exchange, row.calendar, row.status = "NMS", "XNYS", "VERIFIED"
        s.flush()
        return row.id


WIDE_START = date(2000, 1, 3)


def seed_prices(security_id, first, last, dataset_id=None, close=Decimal(100), *, wide=False):
    """Synthetic 24-hour price cache with bars for [first, last]; extends an existing one.

    ``wide`` makes the cache answer any need since 2000 (other days are gaps),
    as a real fetch through today would.
    """
    completed = last_completed_session()
    start, through = (WIDE_START, completed) if wide else (first, min(last, completed))
    with session() as s, s.begin():
        cache = s.get(PriceCache, dataset_id) if dataset_id else s.scalar(
            select(PriceCache).where(PriceCache.security_id == security_id))
        if cache is None:
            stamp = now()
            cache = PriceCache(
                security_id=security_id, start_date=start, end_date=max(last, through),
                complete_through=through, provider="synthetic",
                details={"synthetic": True}, fetched_at=stamp,
                expires_at=stamp + timedelta(hours=24),
            )
            s.add(cache)
            s.flush()
        cache.start_date, cache.end_date = min(cache.start_date, start), max(cache.end_date, last)
        cache.complete_through = max(cache.complete_through, through)
        existing = set(
            s.scalars(select(PriceCacheBar.session_date).where(PriceCacheBar.cache_id == cache.id))
        )
        rows = [
            {
                "cache_id": cache.id,
                "session_date": day,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": Decimal(1),
                "status": "VALID",
            }
            for day in sessions(first, last)
            if day not in existing
        ]
        if rows:
            s.execute(insert(PriceCacheBar), rows)
        return cache.id


def refetch_prices(security_id, first, last, close=Decimal(100), *, wide=True):
    """Simulate a new fetch: the old cache is replaced by a new one (new id)."""
    with session() as s, s.begin():
        old = s.scalar(select(PriceCache).where(PriceCache.security_id == security_id))
        kept = [] if old is None else [
            (row.session_date, row.close) for row in s.scalars(
                select(PriceCacheBar).where(PriceCacheBar.cache_id == old.id))]
        if old is not None:
            s.delete(old)
    identifier = seed_prices(security_id, first, last, close=close, wide=wide)
    with session() as s, s.begin():
        present = set(s.scalars(select(PriceCacheBar.session_date).where(
            PriceCacheBar.cache_id == identifier)))
        rows = [{"cache_id": identifier, "session_date": day, "open": value, "high": value,
                 "low": value, "close": value, "volume": Decimal(1), "status": "VALID"}
                for day, value in kept if day not in present]
        if rows:
            s.execute(insert(PriceCacheBar), rows)
    return identifier


def job_ids(batch_id):
    with session() as s:
        return list(
            s.scalars(
                select(BatchJob.job_id)
                .join(RequestScope, RequestScope.id == BatchJob.scope_id)
                .where(RequestScope.batch_id == batch_id)
            )
        )


def batch(batch_id):
    with session() as s:
        return s.get(Batch, batch_id)


def test_formal_api_persists_batch_before_202_without_probe_or_network(
    lifecycle_client, monkeypatch
):
    from iirp import market_data, providers

    def forbidden(*args, **kwargs):
        raise AssertionError("Provider executed in a local command or GET")

    monkeypatch.setattr(market_data, "fetch_market", forbidden)
    monkeypatch.setattr(providers, "market_probe", forbidden)
    monkeypatch.setattr(providers, "sec_probe", forbidden)
    response = lifecycle_client.post("/api/v1/collections", json=collection())
    assert response.status_code == 202, response.text
    identifier = response.json()["batch_id"]
    engine().dispose()
    assert batch(identifier).status == "QUEUED"
    for path in (
        "/api/v1/batches",
        f"/api/v1/batches/{identifier}",
        "/api/v1/home",
        "/api/v1/coverage?ticker=AAPL",
        "/api/v1/search?q=AAPL",
    ):
        assert lifecycle_client.get(path).status_code == 200
    with session() as s:
        assert s.scalar(select(func.count()).select_from(RequestScope)) == 1
        assert s.scalar(select(func.count()).select_from(Job)) == 0
    invalid = lifecycle_client.post(
        "/api/v1/collections", json={"kind": "market_probe", "request_id": "probe"}
    )
    assert invalid.status_code == 422
    diagnostic = lifecycle_client.post(
        "/api/v1/diagnostics/collections", json={"kind": "fixture_check"}
    )
    assert diagnostic.status_code == 202
    assert "job_id" in diagnostic.json() and "batch_id" not in diagnostic.json()


def test_repeated_request_id_is_idempotent_but_conflicting_parameters_are_rejected():
    params = collection()
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(lambda _: lifecycle.create_collection(params), range(8)))
    assert len({item["batch_id"] for item in result}) == 1
    with pytest.raises(ValueError, match="同一请求标识"):
        lifecycle.create_collection({**params, "end_date": "2023-02-01"})


def test_analysis_request_replay_keeps_frozen_dates_across_day_change(monkeypatch):
    params = AnalysisInput(
        request_id=str(uuid.uuid4()),
        tickers=["AAPL"],
        kind="monthly",
        historical_years=1,
        current_year=2024,
    ).model_dump(mode="json")
    monkeypatch.setattr(lifecycle, "now", lambda: datetime(2024, 1, 2, 22, tzinfo=timezone.utc))
    original = lifecycle.create_analysis(params)
    monkeypatch.setattr(lifecycle, "now", lambda: datetime(2024, 1, 3, 22, tzinfo=timezone.utc))
    replay = lifecycle.create_analysis(params)
    assert replay["batch_id"] == original["batch_id"]
    assert replay["batch"]["params"]["end_date"] == "2024-01-02"


def test_analysis_replay_accepts_new_window_defaults_but_rejects_changed_window():
    params = AnalysisInput(
        request_id=str(uuid.uuid4()),
        tickers=["AAPL"],
        kind="earnings",
        historical_years=8,
        current_year=2026,
    ).model_dump(mode="json")
    old_params = {
        key: value for key, value in params.items() if key not in {"date_window", "date_category"}
    }
    original = lifecycle.create_analysis(old_params)
    replay = lifecycle.create_analysis(params)
    assert replay["id"] == original["id"]
    assert replay["batch_id"] == original["batch_id"]
    with pytest.raises(ValueError, match="同一分析请求标识不能改变参数"):
        lifecycle.create_analysis({**params, "date_window": "before5"})


@pytest.mark.parametrize("action,expected", [("pause", "PAUSED"), ("cancel", "CANCELLED")])
def test_planner_does_not_lock_other_batches_or_overwrite_concurrent_control(
    monkeypatch, action, expected
):
    security_id = seed_security()
    older = lifecycle.create_collection({**collection(), "request_id": str(uuid.uuid4())})
    newer = lifecycle.create_collection(
        {**collection(), "request_id": str(uuid.uuid4()), "end_date": "2023-03-01"}
    )
    with session() as s, s.begin():
        s.get(Batch, older["batch_id"]).created_at = now() - timedelta(days=1)
        for scope in s.scalars(select(RequestScope)):
            scope.security_id = security_id
    planned = []

    def plan(s, scope, batch, capacity):
        planned.append(batch.id)
        assert batch.id == newer["batch_id"]
        # control_batch opens a separate real PostgreSQL connection here. The
        # prior planner locked every candidate batch and timed out at 500 ms.
        controlled = lifecycle.control_batch(older["batch_id"], action)
        assert controlled["batch"]["status"] == expected
        scope.status = "READY"

    monkeypatch.setattr(lifecycle, "_plan_market", plan)
    lifecycle.plan_tick()
    assert planned == [newer["batch_id"]]
    with session() as s:
        older_batch = s.get(Batch, older["batch_id"])
        assert older_batch.status == expected and older_batch.requested_action == action
        assert s.get(Batch, newer["batch_id"]).status == "SUCCEEDED"
        assert s.scalar(select(func.count()).select_from(Job)) == 0


@pytest.mark.parametrize("action,expected", [("pause", "PAUSED"), ("cancel", "CANCELLED")])
def test_same_batch_control_retries_rolled_back_lock_conflict_without_weakening_fence(
    monkeypatch, action, expected
):
    from sqlalchemy.exc import OperationalError

    seed_security()
    created = lifecycle.create_collection({**collection(), "request_id": str(uuid.uuid4())})
    entered, release = threading.Event(), threading.Event()
    conflicts = []
    original = lifecycle._control_batch_once

    def slow_plan(s, scope, batch, capacity):
        entered.set()
        assert release.wait(5)
        scope.status = "RUNNING"

    def control_once(*args):
        try:
            return original(*args)
        except OperationalError as error:
            conflicts.append(error.orig.sqlstate)
            release.set()  # Release only after a real 500 ms lock conflict.
            raise

    monkeypatch.setattr(lifecycle, "_plan_market", slow_plan)
    monkeypatch.setattr(lifecycle, "_control_batch_once", control_once)
    with ThreadPoolExecutor(max_workers=1) as pool:
        planned = pool.submit(lifecycle.plan_tick)
        try:
            assert entered.wait(3)
            controlled = lifecycle.control_batch(created["batch_id"], action)
            assert controlled["batch"]["status"] == expected
            assert conflicts == ["55P03"]
        finally:
            release.set()
        planned.result(timeout=3)
    lifecycle.plan_tick()
    with session() as s:
        batch = s.get(Batch, created["batch_id"])
        assert batch.status == expected and batch.requested_action == action
        assert batch.control_version == 1


def test_planning_rotates_more_than_fifty_waiting_batches_and_prioritizes_control(monkeypatch):
    security_id = seed_security()
    with session() as s, s.begin():
        for index in range(64):
            batch = Batch(
                id=f"rotation-{index}",
                request_id=f"rotation-{index}",
                scope_key=f"rotation-{index}",
                kind="market_history",
                title="Synthetic waiting batch",
                params={},
                status="RUNNING",
                created_at=now() - timedelta(minutes=index),
            )
            s.add(batch)
            s.flush()
            s.add(RequestScope(batch_id=batch.id, symbol="AAPL", security_id=security_id))
        batch.status, batch.requested_action = "PAUSE_REQUESTED", "pause"
        batch.last_planned_at = now()
    planned = []

    def waiting(s, scope, batch, capacity):
        planned.append(batch.id)
        scope.status = "RUNNING"

    monkeypatch.setattr(lifecycle, "_plan_market", waiting)
    lifecycle.plan_tick()
    with session() as s:
        assert s.get(Batch, "rotation-63").status == "PAUSED"
    lifecycle.plan_tick()
    assert set(planned) == {f"rotation-{index}" for index in range(63)}
    with session() as s:
        assert all(batch.last_planned_at for batch in s.scalars(select(Batch)))


def test_repeated_planner_job_lock_conflict_is_visible_and_does_not_block_other_batches(
    monkeypatch,
):
    seed_security()
    good = lifecycle.create_collection({**collection(), "request_id": str(uuid.uuid4())})
    bad = lifecycle.create_collection(
        {**collection(), "request_id": str(uuid.uuid4()), "end_date": "2023-03-01"}
    )
    with session() as s, s.begin():
        s.get(Batch, good["batch_id"]).created_at = now() - timedelta(days=1)
        job = Job(kind="market_history", title="Synthetic held job", idempotency_key="held")
        s.add(job)
        s.flush()
        job_id = job.id
    planned = []

    def plan(s, scope, batch, capacity):
        planned.append(batch.id)
        if batch.id == bad["batch_id"]:
            scope.checkpoint = {"uncommitted_change": True}
            s.flush()
            s.get(Job, job_id, with_for_update=True)
        scope.status = "READY"

    monkeypatch.setattr(lifecycle, "_plan_market", plan)
    with session() as holder, holder.begin():
        holder.get(Job, job_id, with_for_update=True)
        lifecycle.plan_tick()
        lifecycle.plan_tick()
        assert planned.count(bad["batch_id"]) == 1  # Persistent backoff suppresses a hot loop.
        with session() as s, s.begin():
            s.get(Batch, bad["batch_id"]).planning_retry_at = now() - timedelta(seconds=1)
        lifecycle.plan_tick()
        assert planned.count(bad["batch_id"]) == 2
        assert planned.count(good["batch_id"]) == 1
        with session() as s:
            assert s.get(Batch, good["batch_id"]).status == "SUCCEEDED"
            failed = s.get(Batch, bad["batch_id"])
            assert failed.last_planned_at is not None
            scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == failed.id))
            assert scope.checkpoint["planning_conflict"]["sqlstate"] == "55P03"
            assert "uncommitted_change" not in scope.checkpoint
            assert (
                "稍后自动重试" in lifecycle.batch_view(s, failed)["items"][0]["progress"]["stage"]
            )
    with session() as s, s.begin():
        s.get(Batch, bad["batch_id"]).planning_retry_at = now() - timedelta(seconds=1)
    lifecycle.plan_tick()
    with session() as s:
        assert s.get(Batch, bad["batch_id"]).status == "SUCCEEDED"
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == bad["batch_id"]))
        assert "planning_conflict" not in scope.checkpoint


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_control_job_lock_retry_rolls_back_partial_batch_and_link_changes(monkeypatch, action):
    from sqlalchemy.exc import OperationalError

    seed_security()
    created = lifecycle.create_collection({**collection(), "request_id": str(uuid.uuid4())})
    lifecycle.plan_tick()
    token = str(uuid.uuid4())
    with session() as s, s.begin():
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == created["batch_id"]))
        job = lifecycle.linked_jobs(s, scope.id)[0]
        scope_id, job_id = scope.id, job.id
        scope.checkpoint = {"ready_events": 3, "planning_conflict": {"sqlstate": "55P03"}}
        job.status, job.lease_token, job.lease_until = (
            "RUNNING",
            token,
            now() + timedelta(minutes=1),
        )
    original = lifecycle._control_batch_once
    conflicts = []
    with engine().connect() as holder:
        held = holder.begin()
        holder.execute(select(Job.id).where(Job.id == job_id).with_for_update())

        def control_once(*args):
            try:
                return original(*args)
            except OperationalError as error:
                conflicts.append(error.orig.sqlstate)
                # The first attempt already changed Batch and BatchJob before
                # reaching the held Job row. All changes must have rolled back.
                with session() as s:
                    current = s.get(Batch, created["batch_id"])
                    assert current.control_version == 0 and current.requested_action is None
                    assert s.get(BatchJob, (scope_id, job_id)).active is True
                    assert s.get(Job, job_id).lease_token == token
                    assert s.get(RequestScope, scope_id).checkpoint["planning_conflict"]
                held.rollback()
                raise

        monkeypatch.setattr(lifecycle, "_control_batch_once", control_once)
        try:
            lifecycle.control_batch(created["batch_id"], action)
        finally:
            if held.is_active:
                held.rollback()
    assert conflicts == ["55P03"]
    with session() as s:
        current, job = s.get(Batch, created["batch_id"]), s.get(Job, job_id)
        assert current.control_version == job.control_version == 1
        assert current.requested_action == action
        assert current.status == ("PAUSE_REQUESTED" if action == "pause" else "CANCEL_REQUESTED")
        assert job.lease_token == token
        assert s.get(BatchJob, (scope_id, job_id)).active is False
        assert s.get(RequestScope, scope_id).checkpoint == {"ready_events": 3}


def test_two_manual_batches_share_work_and_pause_keeps_the_other_running():
    seed_security()
    first = lifecycle.create_collection(collection(purpose="monthly"))["batch_id"]
    second = lifecycle.create_collection(collection(purpose="interval"))["batch_id"]
    assert first != second
    lifecycle.plan_tick()
    assert job_ids(first) == job_ids(second) and len(job_ids(first)) == 1
    lease = claim({"market_history"})
    assert lease is not None
    result = lifecycle.control_batch(first, "pause")
    assert result["batch"]["status"] == "PAUSED"
    assert fenced(lease, done=1, result={"synthetic": True}, status="SUCCEEDED")
    lifecycle.plan_tick()
    assert batch(first).status == "PAUSED"
    with session() as s:
        first_scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == first))
        second_scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == second))
        assert not s.get(BatchJob, (first_scope.id, lease.id)).active
        assert s.get(BatchJob, (second_scope.id, lease.id)).active


def test_automatic_and_two_manual_subscriptions_keep_independent_control():
    seed_security()
    with session() as s, s.begin():
        automatic, _ = lifecycle._create(
            s, collection(purpose="automatic-update"), trigger="automatic", policy_key="market"
        )
        automatic_id = automatic.id
    first = lifecycle.create_collection(collection(purpose="monthly"))["batch_id"]
    second = lifecycle.create_collection(collection(purpose="interval"))["batch_id"]
    lifecycle.plan_tick()
    assert job_ids(automatic_id) == job_ids(first) == job_ids(second)
    lease = claim({"market_history"})
    lifecycle.update_strategy("market", False)
    assert batch(automatic_id).status == "PAUSED"
    lifecycle.control_batch(first, "cancel")
    assert fenced(lease, checkpoint={"second_manual_remains": True})
    lifecycle.control_batch(second, "pause")
    assert not fenced(lease, status="SUCCEEDED")
    lifecycle.plan_tick()
    assert batch(first).status == "CANCELLED"
    assert batch(second).status == "PAUSED"


def test_dependency_blocks_claim_until_prerequisite_has_succeeded():
    seed_security()
    identifier = lifecycle.create_collection(collection())["batch_id"]
    lifecycle.plan_tick()
    prerequisite_id = job_ids(identifier)[0]
    with session() as s, s.begin():
        dependent = Job(
            kind="research_compute",
            title="Synthetic dependent computation",
            target={},
            idempotency_key="synthetic-dependent",
            priority=0,
        )
        s.add(dependent)
        s.flush()
        dependent_id = dependent.id
        s.add(JobDependency(job_id=dependent_id, prerequisite_id=prerequisite_id))
    assert claim({"research_compute"}) is None
    prerequisite = claim({"market_history"})
    assert fenced(prerequisite, done=1, status="SUCCEEDED")
    assert claim({"research_compute"}).id == dependent_id


def test_same_paused_scope_reuses_batch_without_resuming():
    seed_security()
    params = collection()
    identifier = lifecycle.create_collection(params)["batch_id"]
    lifecycle.plan_tick()
    lifecycle.control_batch(identifier, "pause")
    again = lifecycle.create_collection({**params, "request_id": str(uuid.uuid4())})
    assert again["batch_id"] == identifier and again["reused"]
    assert again["batch"]["status"] == "PAUSED"
    assert claim({"market_history"}) is None
    lifecycle.control_batch(identifier, "resume")
    assert claim({"market_history"}) is not None


def test_paused_scope_request_alias_remains_idempotent_after_resume():
    seed_security()
    params = collection()
    identifier = lifecycle.create_collection(params)["batch_id"]
    lifecycle.plan_tick()
    lifecycle.control_batch(identifier, "pause")
    alias = {**params, "request_id": str(uuid.uuid4())}
    assert lifecycle.create_collection(alias)["batch_id"] == identifier
    lifecycle.control_batch(identifier, "resume")
    assert lifecycle.create_collection(alias)["batch_id"] == identifier


def test_cancel_continue_creates_linked_new_batch_and_retains_cancelled_record():
    seed_security()
    identifier = lifecycle.create_collection(collection())["batch_id"]
    lifecycle.plan_tick()
    old_jobs = job_ids(identifier)
    lifecycle.control_batch(identifier, "cancel")
    assert batch(identifier).status == "CANCELLED"
    child = lifecycle.control_batch(identifier, "continue_remaining")
    assert child["batch_id"] != identifier
    assert child["batch"]["parent_id"] == identifier
    lifecycle.plan_tick()
    assert batch(identifier).status == "CANCELLED"
    assert job_ids(child["batch_id"])
    with session() as s:
        assert all(s.get(Job, job_id) is not None for job_id in old_jobs)


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_control_fence_rejects_business_writer_and_checkpoint(action):
    seed_security()
    identifier = lifecycle.create_collection(collection())["batch_id"]
    lifecycle.plan_tick()
    lease = claim({"market_history"})
    lifecycle.control_batch(identifier, action)
    called = []

    def writer(s, current):
        called.append(current.id)
        s.add(Security(symbol="MUST-NOT-WRITE"))

    assert not fenced(
        lease, business_write=writer, checkpoint={"illicit": True}, status="SUCCEEDED"
    )
    assert not called
    lifecycle.plan_tick()
    assert batch(identifier).status == ("PAUSED" if action == "pause" else "CANCELLED")
    with session() as s:
        assert s.get(Job, lease.id).checkpoint == {}
        assert s.scalar(select(Security).where(Security.symbol == "MUST-NOT-WRITE")) is None


def test_expired_reclaimed_worker_cannot_commit_business_facts():
    seed_security()
    lifecycle.create_collection(collection())
    lifecycle.plan_tick()
    stale = claim({"market_history"})
    with session() as s, s.begin():
        s.get(Job, stale.id).lease_until = now() - timedelta(seconds=1)
    fresh = claim({"market_history"})
    assert fresh.lease_token != stale.lease_token
    called = []
    assert not fenced(stale, business_write=lambda s, current: called.append(current.id))
    assert not called
    assert fenced(fresh, checkpoint={"resumed": True})


def test_business_fact_source_reference_and_checkpoint_commit_atomically():
    seed_security()
    lifecycle.create_collection(collection())
    lifecycle.plan_tick()
    lease = claim({"market_history"})
    source = save_object(b"Synthetic single fenced transaction source")

    def writer(s, current):
        s.add(
            SourceObservation(
                job_id=current.id,
                source_hash=source["sha256"],
                provider="synthetic",
                params={"fixture": True},
            )
        )
        s.flush()  # Real business writers flush facts before returning.

    assert fenced(
        lease,
        source=source,
        business_write=writer,
        checkpoint={"committed": True},
        status="SUCCEEDED",
    )
    with session() as s:
        assert s.get(SourceObject, source["sha256"])
        assert s.scalar(select(SourceObservation).where(SourceObservation.job_id == lease.id))
        assert s.get(Job, lease.id).checkpoint == {"committed": True}


def test_existing_source_is_reused_before_business_foreign_key_flush():
    seed_security()
    lifecycle.create_collection(collection())
    lifecycle.plan_tick()
    lease = claim({"market_history"})
    source = save_object(b"Already known synthetic source object")
    with session() as s, s.begin():
        s.add(SourceObject(**source))

    def writer(s, current):
        s.add(
            SourceObservation(
                job_id=current.id, source_hash=source["sha256"], provider="synthetic", params={}
            )
        )
        s.flush()

    assert fenced(lease, source=source, business_write=writer, status="SUCCEEDED")
    with session() as s:
        assert s.scalar(select(func.count()).select_from(SourceObject)) == 1
        assert s.scalar(select(func.count()).select_from(SourceObservation)) == 1


@pytest.mark.parametrize("change", ["lease_expiry", "control_version"])
def test_fence_rechecks_lease_and_control_after_waiting_for_row_lock(change, monkeypatch):
    from iirp import queue
    from sqlalchemy import event

    seed_security()
    lifecycle.create_collection(collection())
    lifecycle.plan_tick()
    lease = claim({"market_history"})
    clock = [now()]
    monkeypatch.setattr(queue, "now", lambda: clock[0])
    if change == "lease_expiry":
        with session() as s, s.begin():
            s.get(Job, lease.id).lease_until = clock[0] + timedelta(seconds=1)
    entered = threading.Event()
    writes = []
    caller = threading.get_ident()

    def before_lock(conn, cursor, statement, parameters, context, executemany):
        if threading.get_ident() != caller and "FOR UPDATE" in statement:
            entered.set()

    # Observe the actual SELECT boundary, then advance the injected clock.
    # A wall-clock sleep here consumed half the real 500ms lock budget and
    # made scheduler delays fail a test intended to exercise stale-time fencing.
    event.listen(engine(), "before_cursor_execute", before_lock)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with session() as s, s.begin():
                locked = s.get(Job, lease.id, with_for_update=True)
                if change == "control_version":
                    locked.control_version += 1
                    s.flush()
                pending = pool.submit(fenced, lease, business_write=lambda s, current: writes.append(current.id))
                assert entered.wait(1)
                assert not pending.done()
                if change == "lease_expiry":
                    clock[0] += timedelta(seconds=2)
            assert pending.result(timeout=2) is False
    finally:
        event.remove(engine(), "before_cursor_execute", before_lock)
    assert writes == []


def test_business_writer_error_rolls_back_source_fact_and_progress():
    seed_security()
    lifecycle.create_collection(collection())
    lifecycle.plan_tick()
    lease = claim({"market_history"})
    source = save_object(b"Synthetic rollback source")

    def writer(s, current):
        s.add(Security(symbol="ROLLBACK"))
        s.flush()
        raise ValueError("Synthetic failure after business insert")

    with pytest.raises(ValueError, match="Synthetic failure"):
        fenced(lease, source=source, business_write=writer, done=1, checkpoint={"partial": True})
    with session() as s:
        assert s.scalar(select(Security).where(Security.symbol == "ROLLBACK")) is None
        assert s.get(SourceObject, source["sha256"]) is None
        assert s.get(Job, lease.id).progress_done == 0
        assert s.get(Job, lease.id).checkpoint == {}


def test_8_to_12_to_3_reuses_the_cache_and_fetches_one_wider_range():
    security_id = seed_security()
    first = lifecycle.create_collection(
        collection(start_date=None, end_date=None, historical_years=8)
    )["batch_id"]
    with session() as s:
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == first))
        first_start, last = scope.start_date, scope.end_date
    seed_prices(security_id, first_start, last)
    lifecycle.plan_tick()
    assert batch(first).status == "SUCCEEDED"
    assert job_ids(first) == []
    expanded = lifecycle.create_collection(
        collection(start_date=None, end_date=None, historical_years=12)
    )["batch_id"]
    lifecycle.plan_tick()
    with session() as s:
        expansion = s.scalar(select(RequestScope).where(RequestScope.batch_id == expanded))
        jobs = [s.get(Job, identifier) for identifier in job_ids(expanded)]
        # One request for the whole wider range plus a month of buffer.
        assert [job.kind for job in jobs] == ["market_history"]
        assert date.fromisoformat(jobs[0].target["start_date"]) == expansion.start_date - timedelta(days=31)
        assert date.fromisoformat(jobs[0].target["end_date"]) >= last
    shrunk = lifecycle.create_collection(
        collection(start_date=None, end_date=None, historical_years=3)
    )["batch_id"]
    lifecycle.plan_tick()
    assert batch(shrunk).status == "SUCCEEDED"
    assert job_ids(shrunk) == []


def test_gaps_inside_a_covering_cache_are_not_fetched_again():
    security_id = seed_security()
    cache_id = seed_prices(security_id, date(2023, 1, 3), date(2023, 12, 29))
    with session() as s, s.begin():
        for day in (date(2023, 1, 3), date(2023, 12, 29)):
            s.delete(s.get(PriceCacheBar, (cache_id, day)))
    identifier = lifecycle.create_collection(
        collection(start_date="2023-01-03", end_date="2023-12-29", intent="fill_missing")
    )["batch_id"]
    lifecycle.plan_tick()
    assert job_ids(identifier) == []


def test_recovery_preserves_pause_intent_and_restarts_only_remaining_work():
    seed_security()
    identifier = lifecycle.create_collection(collection())["batch_id"]
    lifecycle.plan_tick()
    lease = claim({"market_history"})
    assert fenced(lease, checkpoint={"downloaded_units": 3})
    lifecycle.control_batch(identifier, "pause")
    with session() as s, s.begin():
        s.get(Job, lease.id).lease_until = now() - timedelta(seconds=1)
    with session() as s, s.begin():
        recover(s)
    lifecycle.plan_tick()
    assert batch(identifier).status == "PAUSED"
    lifecycle.control_batch(identifier, "resume")
    fresh = claim({"market_history"})
    assert fresh.checkpoint == {"downloaded_units": 3}
    assert not fenced(lease, checkpoint={"obsolete": True})


def test_paused_history_does_not_block_another_security():
    seed_security("AAPL")
    seed_security("MSFT")
    first = lifecycle.create_collection(collection(start_date="1990-01-01", end_date="2022-12-31"))[
        "batch_id"
    ]
    lifecycle.plan_tick()
    assert len(job_ids(first)) == 1
    lifecycle.control_batch(first, "pause")
    second = lifecycle.create_collection(collection(tickers=["MSFT"]))["batch_id"]
    lifecycle.plan_tick()
    assert job_ids(second), "Paused work must not starve independent runnable collection"


@pytest.mark.parametrize("intent", ["fetch", "refresh"])
def test_future_scope_records_waiting_but_does_not_schedule_future_price_downloads(intent):
    seed_security()
    completed = last_completed_session()
    future = completed + timedelta(days=20)
    identifier = lifecycle.create_collection(
        collection(
            start_date=future.isoformat(),
            end_date=(future + timedelta(days=8)).isoformat(),
            intent=intent,
        )
    )["batch_id"]
    lifecycle.plan_tick()
    with session() as s:
        for identifier in job_ids(identifier):
            job = s.get(Job, identifier)
            assert date.fromisoformat(job.target["end_date"]) <= completed


def test_concurrent_planning_cannot_overwrite_confirmed_pause(monkeypatch):
    seed_security()
    identifier = lifecycle.create_collection(collection())["batch_id"]
    entered, proceed = threading.Event(), threading.Event()
    original = lifecycle._plan_market

    def hold_plan(*args, **kwargs):
        entered.set()
        assert proceed.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "_plan_market", hold_plan)
    with ThreadPoolExecutor(max_workers=2) as pool:
        planning = pool.submit(lifecycle.plan_tick)
        assert entered.wait(3)
        control = pool.submit(lifecycle.control_batch, identifier, "pause")
        # Correct row locking may block control until planning finishes. In the
        # vulnerable interleaving control returns before the planner is released.
        try:
            control.result(timeout=0.2)
        except TimeoutError:
            pass
        finally:
            proceed.set()
        planning.result(timeout=5)
        control.result(timeout=5)
    assert batch(identifier).status == "PAUSED"
    assert claim({"market_history"}) is None


def test_current_schema_matches_head_and_upgrade_preserves_sample_only(lifecycle_client):
    config = Config(str(ROOT / "alembic.ini"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert lifecycle_client.get("/health/ready").json()["migration"] == head
    command.check(config)
    command.downgrade(config, "0001")
    try:
        with session() as s, s.begin():
            s.add(
                Coverage(
                    provider="yfinance",
                    target="AAPL",
                    status="SAMPLE_ONLY",
                    message="Synthetic legacy probe",
                )
            )
        command.upgrade(config, "head")
    finally:
        command.upgrade(config, "head")
    with session() as s:
        assert s.get(Coverage, ("yfinance", "AAPL")).status == "SAMPLE_ONLY"
        assert s.scalar(text("SELECT version_num FROM alembic_version")) == head
        assert s.scalar(select(func.count()).select_from(PriceCache)) == 0
    lifecycle.create_collection(collection())
    with session() as s:
        assert {
            strategy.key for strategy in s.scalars(select(CollectionStrategy)) if strategy.enabled
        } == {"sec", "market"}


def test_analysis_collects_only_completed_session_at_midnight(monkeypatch):
    # New-year holiday midnight must preserve the chosen current year while
    # stopping price collection at the preceding completed exchange session.
    import iirp.analytics.calendar as calendar

    monkeypatch.setattr(lifecycle, "now", lambda: datetime(2026, 1, 1, 6, tzinfo=timezone.utc))
    monkeypatch.setattr(calendar, "last_completed_session", lambda **kw: date(2025, 12, 31))
    result = lifecycle.create_analysis(
        AnalysisInput(request_id="midnight", tickers=["SYNTH"], historical_years=3).model_dump(
            mode="json"
        )
    )
    assert result["batch"]["params"]["end_date"] == "2025-12-31"
    assert result["batch"]["params"]["start_date"].startswith("2022-")


def test_continue_cancelled_analysis_retains_calculation_request():
    original = lifecycle.create_analysis(
        AnalysisInput(request_id="continue-analysis", tickers=["SYNTH"]).model_dump(mode="json")
    )
    lifecycle.control_batch(original["batch_id"], "cancel")
    child = lifecycle.control_batch(original["batch_id"], "continue_remaining")
    again = lifecycle.control_batch(original["batch_id"], "continue_remaining")
    assert again["batch_id"] == child["batch_id"]
    with session() as s:
        request = s.scalar(
            select(AnalysisRequest).where(AnalysisRequest.batch_id == child["batch_id"])
        )
        assert request is not None and request.params == original["params"]
        assert s.get(Batch, original["batch_id"]).status == "CANCELLED"


def test_january_cross_year_request_freezes_the_preceding_start_year(monkeypatch):
    import iirp.analytics.calendar as calendar

    monkeypatch.setattr(lifecycle, "now", lambda: datetime(2026, 1, 10, 6, tzinfo=timezone.utc))
    monkeypatch.setattr(calendar, "last_completed_session", lambda **kw: date(2026, 1, 9))
    values = AnalysisInput(
        request_id="cross-new-year",
        tickers=["SYNTH"],
        kind="interval",
        historical_years=3,
        start_mmdd="12-15",
        end_mmdd="01-20",
    ).model_dump(mode="json")
    created = lifecycle.create_analysis(values)
    assert created["params"]["current_year"] == 2025
    assert created["batch"]["params"]["start_date"] == "2022-12-15"
    monkeypatch.setattr(lifecycle, "now", lambda: datetime(2026, 2, 10, 6, tzinfo=timezone.utc))
    replay = lifecycle.create_analysis(values)
    assert replay["id"] == created["id"]
    assert replay["params"]["current_year"] == 2025


def test_batch_job_pages_include_only_linked_jobs_without_duplicates(lifecycle_client):
    identifier = lifecycle.create_collection(collection(tickers=["AAPL", "MSFT"]))["batch_id"]
    other = lifecycle.create_collection(collection(tickers=["KO"]))["batch_id"]
    lifecycle.plan_tick()
    with session() as s, s.begin():
        scopes = s.scalars(select(RequestScope).where(RequestScope.batch_id == identifier)).all()
        shared = s.scalar(select(BatchJob.job_id).where(BatchJob.scope_id == scopes[0].id))
        s.add(BatchJob(scope_id=scopes[1].id, job_id=shared))
    first = lifecycle_client.get(f"/api/v1/batches/{identifier}/jobs?limit=1").json()
    second = lifecycle_client.get(
        f"/api/v1/batches/{identifier}/jobs",
        params={"limit": 1, "cursor": first["data"]["next_cursor"]},
    ).json()
    ids = [item["id"] for page in (first, second) for item in page["items"]]
    assert len(ids) == len(set(ids)) == 2
    assert not set(ids) & set(job_ids(other))
    assert second["data"]["next_cursor"] == ""
    assert lifecycle_client.get(f"/api/v1/batches/{identifier}/jobs?cursor=bad").status_code == 409
    assert lifecycle_client.get("/api/v1/batches/missing/jobs").status_code == 404


def test_feed_metadata_migration_preserves_payload_and_filter_semantics():
    config = Config(str(ROOT / "alembic.ini"))
    command.downgrade(config, "0003")
    rows = [{"table": "I", "code": "P"}, {"table": "II", "code": "A"}]
    import json

    try:
        with engine().begin() as connection:
            connection.execute(
                text("INSERT INTO issuer (id,name) VALUES ('0000000001','Synthetic')")
            )
            connection.execute(
                text(
                    "INSERT INTO feed_group_revision (id,group_key,issuer_id,accepted_at,data,created_at) "
                    "VALUES ('legacy','legacy','0000000001',now(),CAST(:data AS jsonb),now())"
                ),
                {"data": json.dumps({"transactions": rows})},
            )
        command.upgrade(config, "head")
        with session() as s:
            migrated = s.execute(
                text("SELECT data,match_kinds,row_count FROM feed_group_revision WHERE id='legacy'")
            ).one()
            assert migrated.data == {"transactions": rows}
            assert set(migrated.match_kinds) == {"all", "focus", "buy", "derivative"}
            assert migrated.row_count == 2
    finally:
        command.upgrade(config, "head")


def test_twenty_symbols_keep_eighteen_results_when_one_fails_and_one_needs_review():
    import json

    from iirp.business_worker import _persist, prepare_target
    from iirp.operations import operation

    tickers = [f"SYN{i:02}" for i in range(20)]
    for symbol in tickers[:18]:
        security_id = seed_security(symbol)
        seed_prices(security_id, date(2023, 1, 3), date(2024, 1, 10))
    review = seed_security(tickers[19])
    with session() as s, s.begin():
        s.get(Security, review).status = "NEEDS_REVIEW"
    created = lifecycle.create_analysis(
        AnalysisInput(
            request_id="synthetic-20",
            tickers=tickers,
            kind="interval",
            current_year=2024,
            historical_years=1,
            start_mmdd="01-03",
            end_mmdd="01-10",
        ).model_dump(mode="json")
    )
    lifecycle.plan_tick()
    failed = claim({"market_identity"})
    assert failed.target["symbol"] == tickers[18]
    fenced(failed, status="FAILED", error="Synthetic unavailable identity")
    for _ in range(18):
        lease = claim({"research_compute"})
        assert lease is not None
        payload = operation("research_compute", prepare_target(lease))
        source = save_object(json.dumps(payload).encode())
        assert fenced(
            lease,
            source=source,
            business_write=lambda s, current: _persist(s, current, payload, {}, source),
        )
    lifecycle.plan_tick()
    result = lifecycle.get_analysis(created["id"])
    assert result["status"] == "PARTIAL"
    assert [r["symbol"] for r in result["results"]] == tickers[:18]
    assert len({r["result_id"] for r in result["results"]}) == 18
    statuses = {item["symbol"]: item["status"] for item in result["batch"]["items"]}
    assert statuses[tickers[18]] == "FAILED" and statuses[tickers[19]] == "PARTIAL"
