# One automatic latest demand per day; polling state is one row, not jobs. Synthetic isolated DB only.
from datetime import timedelta

import pytest
from iirp.db import session
from iirp.jobs import lifecycle
from iirp.jobs.auto_update import ensure_fresh, source_status
from iirp.models import Batch, Job, RequestScope, SourcePoll, now
from sqlalchemy import func, select

from tests.jobs.test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401


@pytest.fixture(autouse=True)
def sec_contact_configured(monkeypatch):
    """Scheduling needs a real SEC contact; fetch_sec keeps its own check (CI has none)."""
    monkeypatch.setattr("iirp.jobs.auto_update.sec_configured", lambda: True)
    monkeypatch.setattr("iirp.storage.maintenance.sec_configured", lambda: True)


def polled(seconds_ago):
    with session() as s, s.begin():
        s.merge(SourcePoll(source="sec_latest", last_polled_at=now() - timedelta(seconds=seconds_ago),
                           next_poll_at=now() + timedelta(minutes=30), updated_at=now()))


def poll_requested():
    from iirp.sec.poll import poll_due

    return poll_due()


def test_repeated_opens_reuse_one_daily_demand_and_create_no_jobs():
    first = ensure_fresh({"reason": "open", "sources": ["sec"]})
    for reason in ("scheduler", "open", "visible", "resume"):
        assert ensure_fresh({"reason": reason, "sources": ["sec"]})["batch_ids"] == first["batch_ids"]
    lifecycle.plan_tick()
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Batch)) == 1
        assert s.scalar(select(func.count()).select_from(Job)) == 0
        # Today's demand stays open for the next poll.
        assert s.get(Batch, first["batch_ids"][0]).status == "RUNNING"


def test_opens_pull_the_poll_forward_at_most_every_thirty_seconds():
    polled(5)
    ensure_fresh({"reason": "open", "sources": ["sec"]})
    assert not poll_requested()  # Due 30 seconds after the previous poll, not at once.
    polled(45)  # Time has passed: the pending open request is now due.
    assert poll_requested()


def test_parallel_automatic_ensure_reuses_committed_demand_without_waiting_for_lock():
    first = ensure_fresh({"reason": "open", "sources": ["sec"]})
    with session() as locked, locked.begin():
        lifecycle.advisory(locked, ["freshness", "sec"])
        second = ensure_fresh({"reason": "open", "sources": ["sec"]})
    assert second["batch_ids"] == first["batch_ids"]
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Batch)) == 1


def test_manual_refresh_keeps_personal_demand_and_forces_a_poll():
    automatic = ensure_fresh({"reason": "open", "sources": ["sec"]})
    polled(5)
    manual = ensure_fresh({"reason": "open", "sources": ["sec"], "force": True})
    assert manual["batch_ids"] != automatic["batch_ids"]
    assert poll_requested()
    with session() as s:
        assert s.get(Batch, manual["batch_ids"][0]).trigger == "manual"


def test_polling_lease_shows_checking_and_last_success_is_the_check_time():
    ensure_fresh({"reason": "open", "sources": ["sec"]})
    polled(600)
    with session() as s, s.begin():
        assert source_status(s, "sec")["status"] != "checking"
        row = s.get(SourcePoll, "sec_latest")
        row.lease_token, row.lease_until = "synthetic-lease", now() + timedelta(minutes=1)
        row.last_success_at = now() - timedelta(minutes=1)
        s.flush()
        running = source_status(s, "sec")
        assert running["status"] == "checking"
        assert running["stage"] == "检查最新申报"
        assert running["last_checked_at"] == row.last_success_at.isoformat()


def test_next_day_starts_new_frozen_scope_and_keeps_yesterday_work():
    first = ensure_fresh({"reason": "open", "sources": ["sec"]})
    with session() as s, s.begin():
        batch = s.get(Batch, first["batch_ids"][0])
        batch.created_at = now() - timedelta(days=1)
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch.id))
        lifecycle.add_job(s, scope, "sec_document", {"accession": "synthetic-pending"})
    next_day = ensure_fresh({"reason": "scheduler", "sources": ["sec"]})
    assert next_day["batch_ids"] != first["batch_ids"]
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Batch)) == 2
        old = s.scalar(select(RequestScope).where(RequestScope.batch_id == first["batch_ids"][0]))
        assert any(job.target.get("accession") == "synthetic-pending" for job in lifecycle.linked_jobs(s, old.id))


def test_paused_demand_is_not_replaced_and_is_not_polled(monkeypatch):
    from iirp.sec.poll import claim

    monkeypatch.setattr("iirp.jobs.providers.sec_configured", lambda: True)
    first = ensure_fresh({"reason": "open", "sources": ["sec"]})
    batch_id = first["batch_ids"][0]
    with session() as s, s.begin():
        batch = s.get(Batch, batch_id)
        batch.requested_action, batch.status = "pause", "PAUSED"
    polled(120)
    ensure_fresh({"reason": "scheduler", "sources": ["sec"]})
    with session() as s:
        assert s.get(Batch, batch_id).status == "PAUSED"
        assert s.scalar(select(func.count()).select_from(Batch)) == 1
    assert claim() is None


def test_uncommitted_control_is_not_overwritten_by_an_automatic_opener():
    first = ensure_fresh({"reason": "open", "sources": ["sec"]})
    batch_id = first["batch_ids"][0]
    with session() as control, control.begin():
        batch = control.scalar(select(Batch).where(Batch.id == batch_id).with_for_update())
        batch.requested_action = "pause"
        batch.status = "PAUSE_REQUESTED"
        control.flush()
        # The control holds the freshness-independent batch row; the opener
        # coalesces on the advisory lock and returns readable committed state.
        lifecycle.advisory(control, ["freshness", "sec"])
        assert ensure_fresh({"reason": "scheduler", "sources": ["sec"]})["batch_ids"] == [batch_id]
    with session() as s:
        assert s.get(Batch, batch_id).status == "PAUSE_REQUESTED"
