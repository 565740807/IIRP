"""Public control responses must describe this demand, not historical backoff."""

import json
from datetime import timedelta

import pytest
from iirp import lifecycle
from iirp.business_models import Batch, RequestScope
from iirp.db import session
from iirp.models import Job, now
from iirp.queue import claim
from sqlalchemy import select
from test_lifecycle import (
    clean_lifecycle,
    collection,
    lifecycle_client,
    lifecycle_database,
)

__all__ = ["clean_lifecycle", "lifecycle_client", "lifecycle_database"]


def fail_planning(monkeypatch):
    with monkeypatch.context() as patch:
        def fail(*_):
            raise ValueError("synthetic planning failure")
        patch.setattr(lifecycle, "_plan_batch", fail)
        lifecycle.plan_tick()


def read_batch(client, identifier):
    response = client.get(f"/api/v1/batches/{identifier}")
    assert response.status_code == 200
    return response.json()["batch"]


@pytest.mark.parametrize("action", ["pause", "cancel"])
@pytest.mark.parametrize("work", ["unplanned", "running", "shared"])
def test_planning_backoff_control_response_and_reread(monkeypatch, lifecycle_client, action, work):
    identifier = lifecycle.create_collection(collection(tickers=["SYNTH"]))["batch_id"]
    other = None
    if work != "unplanned":
        lifecycle.plan_tick()
        if work == "shared":
            # Different demand, same identity job.
            other = lifecycle.create_collection(collection(tickers=["SYNTH"], end_date="2023-02-10"))["batch_id"]
            lifecycle.plan_tick()
        assert claim({"market_identity"}) is not None
    with session() as s, s.begin():
        s.get(Batch, identifier).last_planned_at = now() - timedelta(days=1)
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == identifier))
        completed = lifecycle.add_job(s, scope, "synthetic_completed", {"evidence": "frozen"})
        completed.status, completed.result, completed.finished_at = "SUCCEEDED", {"frozen": [1, 2, 3]}, now()
        completed_id = completed.id
    fail_planning(monkeypatch)
    before = read_batch(lifecycle_client, identifier)
    assert before["items"][0]["progress"]["retry_at"]
    response = lifecycle_client.post(f"/api/v1/batches/{identifier}/actions", json={"action": action})
    assert response.status_code == 200
    expected = ("PAUSE_REQUESTED" if action == "pause" else "CANCEL_REQUESTED") if work == "running" else (
        "PAUSED" if action == "pause" else "CANCELLED")
    views = [response.json()["batch"], read_batch(lifecycle_client, identifier)]
    print(json.dumps({"action": action, "work": work, "views": views}, ensure_ascii=False))
    for view in views:
        assert view["status"] == expected
        assert view["items"][0]["jobs_done"] == 1
        progress = view["items"][0]["progress"]
        assert progress["activity_status"] == expected
        assert ("暂停" if action == "pause" else "取消") in progress["stage"]
        assert "自动重试" not in progress["stage"]
        assert progress["retry_at"] is None
        assert progress["planning_error"]["type"] == "ValueError"
    with session() as s:
        assert s.get(Batch, identifier).planning_retry_at is None
        assert s.get(Job, completed_id).result == {"frozen": [1, 2, 3]}
    if other:
        assert read_batch(lifecycle_client, other)["activity_status"] == "RUNNING"
    if action == "pause" and work != "running":
        resumed = lifecycle_client.post(f"/api/v1/batches/{identifier}/actions", json={"action": "resume"})
        assert resumed.status_code == 200
        for view in (resumed.json()["batch"], read_batch(lifecycle_client, identifier)):
            progress = view["items"][0]["progress"]
            assert "自动重试" not in progress["stage"] and "暂停" not in progress["stage"]
            assert progress["retry_at"] is None
            assert progress["activity_status"] == ("RUNNING" if other else "WAITING")
        lifecycle.plan_tick()
        assert read_batch(lifecycle_client, identifier)["items"][0]["progress"]["planning_error"] is None


def test_explicit_retry_replaces_historical_planning_schedule(monkeypatch, lifecycle_client):
    identifier = lifecycle.create_collection(collection(tickers=["SYNTH"]))["batch_id"]
    fail_planning(monkeypatch)
    with session() as s, s.begin():
        s.get(Batch, identifier).status = "FAILED"
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == identifier))
        scope.status = "FAILED"
    response = lifecycle_client.post(f"/api/v1/batches/{identifier}/actions", json={"action": "retry_failed"})
    assert response.status_code == 200
    for view in (response.json()["batch"], read_batch(lifecycle_client, identifier)):
        progress = view["items"][0]["progress"]
        assert "自动重试" not in progress["stage"] and progress["retry_at"] is None
        assert progress["planning_error"]["type"] == "ValueError"
    fail_planning(monkeypatch)
    progress = read_batch(lifecycle_client, identifier)["items"][0]["progress"]
    assert "自动重试" in progress["stage"] and progress["retry_at"]
    assert progress["planning_error"]["attempts"] == 2
