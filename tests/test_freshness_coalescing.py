# Bounded latest rounds retain durable subscriptions; synthetic isolated DB only.
from datetime import timedelta

import pytest
from iirp import lifecycle
from iirp.business_models import Batch, BatchJob, CollectionStrategy, RequestScope
from iirp.db import session
from iirp.freshness import ensure_fresh, source_status
from iirp.models import Job, now
from sqlalchemy import func, select
from test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401


@pytest.fixture(autouse=True)
def sec_contact_configured(monkeypatch):
    """Scheduling needs a real SEC contact; fetch_sec keeps its own check (CI has none)."""
    monkeypatch.setattr("iirp.freshness.sec_configured", lambda: True)
    monkeypatch.setattr("iirp.maintenance.sec_configured", lambda: True)


def ready_head(batch_id, round_number):
    with session() as s, s.begin():
        batch = s.get(Batch, batch_id)
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch_id))
        if round_number == 0:
            head = lifecycle.add_job(s, scope, "sec_discover", {"mode": "latest", "round": "initial"})
            lifecycle.add_job(s, scope, "sec_document", {"accession": "synthetic-pending"})
        else:
            head = s.scalar(select(Job).join(BatchJob).where(BatchJob.scope_id == scope.id,
                            Job.kind == "sec_discover", Job.status == "QUEUED"))
        head.status = "SUCCEEDED"
        head.finished_at = now() - timedelta(minutes=2)
        head.checkpoint = {"sec_scan": {"complete": True, "newest_accepted_at": "2026-09-14T20:00:00+00:00"}}
        batch.created_at = now() - timedelta(minutes=2)
        policy = s.get(CollectionStrategy, "sec")
        policy.options = {**policy.options, "latest_requested_at": batch.created_at.isoformat()}


def test_many_head_rounds_coalesce_without_losing_old_documents_or_watermark():
    first = ensure_fresh({"reason": "open", "sources": ["sec"]})
    batch_id = first["batch_ids"][0]
    for round_number in range(3):
        ready_head(batch_id, round_number)
        next_round = ensure_fresh({"reason": "scheduler", "sources": ["sec"]})
        assert next_round["batch_ids"] == [batch_id]
        assert ensure_fresh({"reason": "open", "sources": ["sec"]})["batch_ids"] == [batch_id]
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Batch)) == 1
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch_id))
        jobs = lifecycle.linked_jobs(s, scope.id)
        assert len([job for job in jobs if job.kind == "sec_discover"]) == 4
        assert len([job for job in jobs if job.status == "QUEUED" and job.kind == "sec_discover"]) == 1
        assert any(job.target.get("accession") == "synthetic-pending" and job.status == "QUEUED" for job in jobs)
        assert scope.checkpoint["latest_target"]["watermark"] == "2026-09-14T20:00:00+00:00"


def test_parallel_automatic_ensure_reuses_committed_demand_without_waiting_for_lock():
    first = ensure_fresh({"reason": "open", "sources": ["sec"]})
    with session() as locked, locked.begin():
        lifecycle.advisory(locked, ["freshness", "sec"])
        second = ensure_fresh({"reason": "open", "sources": ["sec"]})
    assert second["batch_ids"] == first["batch_ids"]
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Batch)) == 1


def test_manual_refresh_keeps_personal_demand_and_shares_active_source_head():
    automatic = ensure_fresh({"reason": "open", "sources": ["sec"]})
    with session() as s, s.begin():
        # Different original minute rounds must still share the live source job.
        s.get(Batch, automatic["batch_ids"][0]).created_at = now() - timedelta(minutes=2)
    lifecycle.plan_tick()
    manual = ensure_fresh({"reason": "open", "sources": ["sec"], "force": True})
    assert manual["batch_ids"] != automatic["batch_ids"]
    with session() as s:
        assert s.get(Batch, manual["batch_ids"][0]).trigger == "manual"
    lifecycle.plan_tick()
    with session() as s:
        scopes = s.scalars(select(RequestScope)).all()
        heads = [{job.id for job in lifecycle.linked_jobs(s, scope.id) if job.kind == "sec_discover"} for scope in scopes]
        assert len(heads) == 2 and heads[0] == heads[1]


def test_waiting_source_has_no_fake_running_stage_until_a_live_lease_exists():
    first = ensure_fresh({"reason": "open", "sources": ["sec"]})
    lifecycle.plan_tick()
    with session() as s, s.begin():
        waiting = source_status(s, "sec")
        assert waiting["status"] == "waiting"
        assert waiting["stage"] == "等待 SEC 通道"
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == first["batch_ids"][0]))
        head = next(job for job in lifecycle.linked_jobs(s, scope.id) if job.kind == "sec_discover")
        for index in range(25):
            lifecycle.add_job(s, scope, "sec_document", {"accession": f"preview-{index}"})
        head.status, head.lease_token, head.lease_until = "RUNNING", "test-fenced-lease", now() + timedelta(minutes=1)
        s.flush()
        running = source_status(s, "sec")
        assert running["status"] == "checking"
        assert running["stage"] == "检查申报索引"


def test_next_day_starts_new_frozen_scope_and_keeps_yesterday_work():
    first = ensure_fresh({"reason": "open", "sources": ["sec"]})
    ready_head(first["batch_ids"][0], 0)
    with session() as s, s.begin():
        batch = s.get(Batch, first["batch_ids"][0])
        batch.created_at = now() - timedelta(days=1)
    next_day = ensure_fresh({"reason": "scheduler", "sources": ["sec"]})
    assert next_day["batch_ids"] != first["batch_ids"]
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Batch)) == 2
        old = s.scalar(select(RequestScope).where(RequestScope.batch_id == first["batch_ids"][0]))
        assert any(job.target.get("accession") == "synthetic-pending" for job in lifecycle.linked_jobs(s, old.id))


def test_head_rollover_does_not_overwrite_a_concurrent_batch_control():
    first = ensure_fresh({"reason": "open", "sources": ["sec"]})
    batch_id = first["batch_ids"][0]
    ready_head(batch_id, 0)
    with session() as control, control.begin():
        batch = control.scalar(select(Batch).where(Batch.id == batch_id).with_for_update())
        batch.requested_action = "pause"
        batch.status = "PAUSE_REQUESTED"
        control.flush()
        # The uncommitted control owns the batch row. An automatic opener skips
        # rollover and returns readable committed state without blocking it.
        assert ensure_fresh({"reason": "scheduler", "sources": ["sec"]})["batch_ids"] == [batch_id]
    with session() as s:
        assert s.get(Batch, batch_id).status == "PAUSE_REQUESTED"
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "sec_discover")) == 1


def test_pending_yesterday_head_does_not_hide_todays_new_scope():
    today = now().astimezone(lifecycle.ET).date()
    yesterday = today - timedelta(days=1)
    first = ensure_fresh({"reason": "open", "sources": ["sec"]})
    with session() as s, s.begin():
        batch = s.get(Batch, first["batch_ids"][0])
        batch.created_at = now() - timedelta(days=1)
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch.id))
        head = lifecycle.add_job(s, scope, "sec_discover", {
            "mode": "latest", "round": "yesterday", "max_pages": 1,
            "start_date": str(yesterday - timedelta(days=3)), "end_date": str(yesterday),
        })
        scope.checkpoint = {**(scope.checkpoint or {}), "latest_target": dict(head.target)}
        old_scope, old_head = scope.id, head.id
        policy = s.get(CollectionStrategy, "sec")
        policy.options = {**policy.options, "latest_requested_at": batch.created_at.isoformat()}
    next_day = ensure_fresh({"reason": "scheduler", "sources": ["sec"]})
    assert next_day["batch_ids"] != first["batch_ids"]
    lifecycle.plan_tick()
    with session() as s:
        assert s.get(BatchJob, (old_scope, old_head)).active
        assert s.get(Job, old_head).status == "QUEUED"
        assert s.get(Job, old_head).target["end_date"] == str(yesterday)
        current = s.scalar(select(RequestScope).where(RequestScope.batch_id == next_day["batch_ids"][0]))
        heads = [job for job in lifecycle.linked_jobs(s, current.id) if job.kind == "sec_discover"]
        assert len(heads) == 1 and heads[0].target["end_date"] == str(today)
        assert heads[0].id != old_head


def test_same_day_round_does_not_subscribe_to_unrelated_yesterday_head():
    today = now().astimezone(lifecycle.ET).date()
    yesterday = today - timedelta(days=1)
    first = ensure_fresh({"reason": "open", "sources": ["sec"]})
    batch_id = first["batch_ids"][0]
    ready_head(batch_id, 0)
    with session() as s, s.begin():
        old = Batch(request_id="yesterday-demand", scope_key="yesterday-demand", kind="sec_latest",
                    title="synthetic older discovery", params={}, trigger="automatic", status="RUNNING",
                    created_at=now() - timedelta(days=1))
        s.add(old)
        s.flush()
        scope = RequestScope(batch_id=old.id, symbol="SEC", status="RUNNING")
        s.add(scope)
        s.flush()
        old_head = lifecycle.add_job(s, scope, "sec_discover", {
            "mode": "latest", "round": "yesterday", "start_date": str(yesterday - timedelta(days=3)),
            "end_date": str(yesterday), "max_pages": 1,
        }).id
    assert ensure_fresh({"reason": "scheduler", "sources": ["sec"]})["batch_ids"] == [batch_id]
    with session() as s:
        current = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch_id))
        assert current.checkpoint["latest_target"]["end_date"] == str(today)
        assert s.get(BatchJob, (current.id, old_head)) is None
        assert s.get(Job, old_head).status == "QUEUED"
