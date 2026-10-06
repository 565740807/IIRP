"""Real PostgreSQL fault injection; no real provider or live worker is touched."""
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from iirp.db import engine, session
from iirp.jobs import handlers, lifecycle
from iirp.jobs.operation_pool import OperationChild, OperationInterrupted
from iirp.jobs.ownership import OwnershipLost, WorkerOwnership, previous_leases_drained
from iirp.jobs.queue import claim, fenced, recover
from iirp.models import Batch, Job, RequestScope, now
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from tests.jobs.test_lifecycle import (  # Reuse the independently named disposable DB fixture.
    clean_lifecycle,
    collection,
    lifecycle_database,
)

# pytest discovers imported fixtures; explicit aliases keep lint and intent clear.
__all__ = ["lifecycle_database", "clean_lifecycle"]


def isolated_connection():
    conn = engine().connect()
    assert conn.scalar(text("SELECT current_database()")).startswith("iirp_v1_test_")
    conn.commit()
    return conn


@pytest.mark.parametrize("failure", ["timeout", "data"])
def test_batch_failure_rolls_back_backs_off_and_other_job_publishes(monkeypatch, failure):
    bad = lifecycle.create_collection(collection(tickers=["BAD"]))["batch_id"]
    good = lifecycle.create_collection(collection(tickers=["GOOD"]))["batch_id"]
    original = lifecycle._plan_batch
    attempted = []

    def inject(s, batch, *args):
        if batch.id == bad:
            attempted.append(batch.id)
            scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == bad))
            scope.checkpoint = {"must_rollback": True}
            s.flush()
            if failure == "timeout":
                s.execute(text("SET LOCAL statement_timeout = '20ms'"))
                s.execute(text("SELECT pg_sleep(0.1)"))
            raise ValueError("synthetic invalid batch")
        return original(s, batch, *args)

    monkeypatch.setattr(lifecycle, "_plan_batch", inject)
    lifecycle.plan_tick()
    lifecycle.plan_tick()
    with session() as s:
        failed = s.get(Batch, bad)
        assert failed.planning_failures == 1
        assert failed.planning_retry_at > now()
        assert failed.planning_error["sqlstate"] == ("57014" if failure == "timeout" else None)
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == bad))
        assert "must_rollback" not in scope.checkpoint
        assert scope.checkpoint["planning_conflict"]["attempts"] == 1
        progress = lifecycle.batch_view(s, failed)["items"][0]["progress"]
        assert progress["planning_error"]["attempts"] == 1
        assert progress["retry_at"] == failed.planning_retry_at.isoformat()
        assert ("执行期限" if failure == "timeout" else "数据或规则异常") in progress["stage"]
        assert s.get(Batch, good).last_planned_at
    assert attempted == [bad]
    job = claim({"market_identity"})
    assert job is not None and job.target["symbol"] == "GOOD"
    assert fenced(job, status="SUCCEEDED", result={"verified": "independent queue advanced"})
    with session() as s, s.begin():
        s.get(Batch, bad).planning_retry_at = now() - timedelta(seconds=1)
    lifecycle.plan_tick()
    with session() as s:
        assert s.get(Batch, bad).planning_failures == 2
        assert s.get(Job, job.id).status == "SUCCEEDED"
    # User controls are immediate even during backoff.
    lifecycle.control_batch(bad, "cancel")
    with session() as s:
        assert s.get(Batch, bad).status == "CANCELLED"


def test_terminated_database_connection_is_not_misreported_as_batch_error(monkeypatch):
    bad = lifecycle.create_collection(collection(tickers=["BAD"]))["batch_id"]

    def terminate(s, batch, *args):
        pid = s.scalar(text("SELECT pg_backend_pid()"))
        with isolated_connection() as killer:
            assert killer.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
            killer.commit()
        s.execute(text("SELECT 1"))

    monkeypatch.setattr(lifecycle, "_plan_batch", terminate)
    with pytest.raises(OperationalError):
        lifecycle.plan_tick()
    with session() as s:
        assert s.get(Batch, bad).planning_error is None
        assert s.get(Batch, bad).planning_failures == 0


def test_singleton_loss_stops_real_child_and_successor_waits_for_old_lease(tmp_path):
    lifecycle.create_collection(collection(tickers=["SYNTH"]))
    lifecycle.plan_tick()
    job = claim({"market_identity"})
    assert job is not None
    child = OperationChild()
    started = threading.Event()
    with WorkerOwnership(interval=0.02) as old:
        # A real blocked child, no network, under the real OperationChild supervisor.
        child._start()
        os.killpg(child.proc.pid, signal.SIGSTOP)
        proc = child.proc
        def checkpoint():
            started.set()
            return not old.stopping()
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(child.run, "market_identity", {}, checkpoint)
            assert started.wait(5)
            with isolated_connection() as killer:
                assert killer.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": old.pid})
                killer.commit()
            assert old.lost.wait(5)
            with pytest.raises(OwnershipLost):
                old.require()
            with WorkerOwnership(interval=0.02) as successor:
                successor.require()
                assert not previous_leases_drained()
                with pytest.raises(OperationInterrupted):
                    running.result(timeout=6)
                assert proc.poll() is not None
                # Recovery runs only after lease expiration; no early capacity overlap.
                with session() as s, s.begin():
                    s.get(Job, job.id).lease_until = now() - timedelta(seconds=1)
                    s.flush()
                    recover(s)
                assert previous_leases_drained()
                replacement = claim({"market_identity"})
                assert replacement and replacement.id == job.id
                assert replacement.lease_token != job.lease_token
                assert not fenced(job, status="SUCCEEDED", result={"late": True})
                assert fenced(replacement, status="SUCCEEDED", result={"successor": True})


def test_stopping_prevents_completed_response_publication(monkeypatch):
    lifecycle.create_collection(collection(tickers=["SYNTH"]))
    lifecycle.plan_tick()
    job = claim({"market_identity"})
    stopped = threading.Event()
    class CompletedAtLoss:
        def run(self, *args):
            stopped.set()
            return {"ok": True, "data": {"symbol": "SYNTH", "currency": "USD"}}
    handlers.execute_business(job, stopped.is_set, runner=CompletedAtLoss())
    with session() as s:
        assert s.get(Job, job.id).status == "RUNNING"
        assert not s.get(Job, job.id).result


def test_ownership_health_expiry_is_irreversible():
    with WorkerOwnership() as owner:
        owner.verified_at = time.monotonic() - 3
        assert owner.stopping()
        owner.verified_at = time.monotonic()
        with pytest.raises(OwnershipLost):
            owner.require()


def test_real_coordinator_exits_after_singleton_connection_loss(tmp_path):
    from iirp.jobs.queue import create_job
    first, _ = create_job("fixture_check", {"test": "owner-first"})
    second, _ = create_job("fixture_check", {"test": "owner-second"})
    # Only automatic scheduling/provider discovery is disabled. The real main
    # loop, executor, lease writes, singleton monitor and claim code all run.
    script = """
from iirp.jobs import auto_update
from iirp.jobs import lifecycle
from iirp.storage import maintenance
from iirp.jobs import worker
auto_update.ensure_fresh = lambda *a, **k: None
lifecycle.plan_tick = lambda *a, **k: None
maintenance.schedule_tick = lambda *a, **k: None
maintenance.operational_log_tick = lambda *a, **k: None
worker.main()
"""
    output = tmp_path / "coordinator.txt"
    with output.open("w") as log:
        proc = subprocess.Popen([sys.executable, "-c", script], stdout=log, stderr=log,
                                env={**os.environ, "IIRP_RUNTIME_DIR": str(tmp_path)})
        try:
            deadline = time.monotonic() + 12
            pid = None
            while time.monotonic() < deadline:
                with session() as s:
                    if s.get(Job, first.id).status == "RUNNING":
                        pid = s.scalar(text("SELECT pid FROM pg_locks WHERE locktype='advisory' AND classid=0 AND objid=482918001 AND granted AND database=(SELECT oid FROM pg_database WHERE datname=current_database())"))
                        break
                assert proc.poll() is None, output.read_text()
                time.sleep(0.05)
            assert pid is not None
            with isolated_connection() as killer:
                assert killer.scalar(text("SELECT datname=current_database() FROM pg_stat_activity WHERE pid=:pid"), {"pid": pid})
                assert killer.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
                killer.commit()
            assert proc.wait(timeout=8) != 0
            with session() as s:
                assert s.get(Job, first.id).status == "RUNNING"  # Durable lease awaits recovery.
                assert s.get(Job, second.id).status == "QUEUED"  # No additional claim.
            assert "OwnershipLost" in output.read_text()
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=8)


def test_loss_at_publication_rolls_back_source_and_lease(monkeypatch):
    from iirp.models import SourceObject
    lifecycle.create_collection(collection(tickers=["SYNTH"]))
    lifecycle.plan_tick()
    job = claim({"market_identity"})
    stopped = threading.Event()
    original = handlers.fenced
    observed_lease = []
    def stop_at_write(job, **kwargs):
        if kwargs.get("business_write"):
            with session() as s:
                observed_lease.append(s.get(Job, job.id).lease_until)
            stopped.set()
        return original(job, **kwargs)
    monkeypatch.setattr(handlers, "fenced", stop_at_write)
    class Completed:
        def run(self, *args):
            return {"ok": True, "data": {"symbol": "SYNTH", "currency": "USD"}}
    handlers.execute_business(job, stopped.is_set, runner=Completed())
    with session() as s:
        current = s.get(Job, job.id)
        assert current.status == "RUNNING" and not current.result
        assert current.lease_until == observed_lease[0]
        assert not s.scalar(select(SourceObject.sha256).limit(1))


def test_batch_error_visible_when_scope_error_projection_is_locked():
    created = lifecycle.create_collection(collection(tickers=["SCOPELOCK"]))["batch_id"]
    with session() as holder, holder.begin():
        scope = holder.scalar(select(RequestScope).where(RequestScope.batch_id == created).with_for_update())
        lifecycle.plan_tick()
        with session() as s:
            failed = s.get(Batch, created)
            assert failed.planning_error["sqlstate"] == "55P03"
            assert not s.get(RequestScope, scope.id).checkpoint.get("planning_conflict")
            # API projection falls back to durable batch metadata even when
            # another scope writer prevented its optional checkpoint copy.
            progress = lifecycle.batch_view(s, failed)["items"][0]["progress"]
            assert progress["planning_error"]["sqlstate"] == "55P03"
            assert progress["retry_at"] == failed.planning_retry_at.isoformat()
