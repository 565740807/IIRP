"""Completion wakeups survive conflicts, rollback and stale leases (isolated DB)."""
from datetime import timedelta
from uuid import uuid4

import pytest
from iirp.db import session
from iirp.jobs import lifecycle
from iirp.jobs.queue import claim, fenced
from iirp.jobs.signals import signal_batch
from iirp.models import Batch, BatchPlanSignal, Job, RequestScope, now
from sqlalchemy import delete, select

from tests.jobs.test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_database,
    seed_security,
)


def pending_quote():
    seed_security("^VIX")
    result = lifecycle.create_collection({"kind": "market_quotes", "request_id": str(uuid4()), "tickers": ["^VIX"]})
    lifecycle.plan_tick()
    return result["batch_id"], claim({"market_quote"})


def test_completion_notification_survives_planner_lock_and_process_boundary():
    batch_id, job = pending_quote()
    assert fenced(job, status="SUCCEEDED", done=1)
    with session() as s:
        assert s.get(BatchPlanSignal, batch_id)
    with session() as blocker, blocker.begin():
        lifecycle.advisory(blocker, ["planner_capacity"])
        lifecycle.plan_job_scopes(job.id)
    with session() as s:
        assert s.get(BatchPlanSignal, batch_id)
        assert s.get(Batch, batch_id).status == "RUNNING"
    # A fresh planner, without the completing worker's in-memory context, converges.
    lifecycle.plan_tick()
    with session() as s:
        assert s.get(Batch, batch_id).status == "SUCCEEDED"
        assert s.get(BatchPlanSignal, batch_id) is None


def test_wakeup_and_result_roll_back_together(monkeypatch):
    import iirp.jobs.signals as signals
    batch_id, job = pending_quote()
    def rejected(*_):
        raise RuntimeError("synthetic transaction interruption")
    monkeypatch.setattr(signals, "signal_job", rejected)
    with pytest.raises(RuntimeError, match="interruption"):
        fenced(job, status="SUCCEEDED", done=1)
    with session() as s:
        assert s.get(Job, job.id).status == "RUNNING"
        assert s.get(BatchPlanSignal, batch_id) is None


def test_obsolete_lease_cannot_publish_or_signal():
    batch_id, job = pending_quote()
    with session() as s, s.begin():
        s.get(Job, job.id).lease_token = str(uuid4())
    assert not fenced(job, status="SUCCEEDED")
    with session() as s:
        assert s.get(BatchPlanSignal, batch_id) is None
        assert s.get(Job, job.id).status == "RUNNING"


def test_new_notification_is_not_deleted_by_older_planning_pass(monkeypatch):
    batch_id, job = pending_quote()
    assert fenced(job, status="SUCCEEDED")
    original = lifecycle._plan_batch
    def newer(s, batch, *args):
        with session() as concurrent, concurrent.begin():
            signal_batch(concurrent, batch.id)
        return original(s, batch, *args)
    monkeypatch.setattr(lifecycle, "_plan_batch", newer)
    lifecycle.plan_job_scopes(job.id)
    with session() as s:
        assert s.get(BatchPlanSignal, batch_id) is not None
        assert s.get(Batch, batch_id).status == "SUCCEEDED"


def test_fresh_completion_not_stuck_behind_large_old_rotation():
    batch_id, job = pending_quote()
    with session() as s, s.begin():
        for index in range(300):
            s.add(Batch(id=str(uuid4()), request_id=f"older-{index}", scope_key=f"older-{index}",
                        kind="market_quotes", title="synthetic old demand", params={},
                        status="RUNNING", trigger="automatic", created_at=now()-timedelta(days=2)))
        # Remove creation signals; simulate an already deployed legacy backlog.
        s.execute(delete(BatchPlanSignal))
    assert fenced(job, status="SUCCEEDED")
    lifecycle.plan_tick(time_budget_seconds=0)
    with session() as s:
        assert s.get(Batch, batch_id).status == "SUCCEEDED"
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch_id))
        assert scope.status == "READY"


def test_planner_parent_lock_does_not_block_transactional_completion_signal():
    batch_id, job = pending_quote()
    with session() as planning, planning.begin():
        planning.scalar(select(Batch).where(Batch.id == batch_id)
                        .with_for_update(key_share=True))
        assert fenced(job, status="SUCCEEDED", done=1)
    with session() as s:
        assert s.get(BatchPlanSignal, batch_id)
        assert s.get(Job, job.id).status == "SUCCEEDED"
