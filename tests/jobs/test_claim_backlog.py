"""Large active-demand graph in a disposable database, with real claim locking."""
from uuid import uuid4

import pytest
from iirp.api.schemas import CollectionInput
from iirp.db import session
from iirp.jobs import lifecycle
from iirp.jobs.queue import claim
from iirp.models import Batch, BatchJob, Job, RequestScope
from sqlalchemy import insert, select, text

from tests.jobs.test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401

pytestmark = pytest.mark.slow


def test_claim_with_eighty_four_thousand_active_subscriptions():
    batch = lifecycle.create_collection(CollectionInput.model_validate({
        "request_id": str(uuid4()), "kind": "sec_history",
    }).model_dump(mode="json"))
    with session() as s, s.begin():
        scopes = [str(uuid4()) for _ in range(60)]
        s.execute(insert(RequestScope), [{"id": i, "batch_id": batch["batch_id"],
                   "symbol": f"FIXTURE{k}"} for k, i in enumerate(scopes)])
        jobs = [str(uuid4()) for _ in range(1400)]
        s.execute(insert(Job), [{"id": i, "kind": "sec_discover", "title": "Synthetic backlog",
                 "target": {"fixture": k}, "idempotency_key": i, "trigger": "manual",
                 "priority": 30, "progress_total": 1} for k, i in enumerate(jobs)])
        s.execute(insert(BatchJob), [{"scope_id": scope, "job_id": job, "active": True}
                                    for scope in scopes for job in jobs])
        s.execute(text("ANALYZE batch_job"))
        s.execute(text("ANALYZE job"))
        # Same shared work can have a higher-priority active subscription.
        current = s.get(Batch, batch["batch_id"])
        current.kind = "sec_latest"
    # Default 5s DB timeout remains in force; the old repeated full scans time out.
    first = claim({"sec_discover"})
    assert first is not None and first.status == "RUNNING" and first.attempts == 1
    second = claim({"sec_discover"})
    assert second is not None and second.id != first.id
    with session() as s:
        assert s.scalar(select(Job.status).where(Job.id == first.id)) == "RUNNING"


def test_recent_manual_research_is_planned_despite_old_background_and_full_source_queue():
    from datetime import date, timedelta

    from iirp.models import now

    from tests.jobs.test_lifecycle import collection, job_ids, seed_security
    seed_security("META")
    with session() as s, s.begin():
        for i in range(80):
            s.add(Batch(id=f"auto-{i}", request_id=f"auto-{i}", scope_key=f"auto-{i}",
                        kind="sec_history", title="Synthetic automatic work", params={},
                        trigger="automatic", last_planned_at=now()-timedelta(hours=1)))
        s.flush()
        auto_scope = RequestScope(batch_id="auto-0", symbol="SEC", start_date=date(2026, 9, 1), end_date=date(2026, 9, 1))
        s.add(auto_scope)
        s.flush()
        for i in range(32):
            lifecycle.add_job(s, auto_scope, "market_history", {"symbol": "OTHER", "part": i}, 0)
    request = lifecycle.create_collection(collection(tickers=["META"]))
    with session() as s, s.begin():
        s.get(Batch, request["batch_id"]).last_planned_at = now()
    lifecycle.plan_tick()
    assert job_ids(request["batch_id"]), "Manual demand must bypass old automatic planning rounds"
    selected = claim({"market_history"})
    assert selected.target["symbol"] == "META", "Manual demand precedes automatic priority-zero work"


def test_worker_planning_budget_leaves_both_foreground_and_background_an_opportunity(monkeypatch):
    with session() as s, s.begin():
        for i in range(8):
            s.add(Batch(id=f"bounded-{i}", request_id=f"bounded-{i}", scope_key=f"bounded-{i}",
                        kind="sec_history", title="Synthetic", params={}, trigger="manual" if i == 0 else "automatic"))
    clock = [0.0]
    planned = []
    monkeypatch.setattr(lifecycle.time, "monotonic", lambda: clock[0])
    def slow(identifier, *_):
        planned.append(identifier)
        clock[0] += 2
    monkeypatch.setattr(lifecycle, "_plan_one", slow)
    lifecycle.plan_tick(time_budget_seconds=1)
    assert len(planned) == 2 and planned[0] == "bounded-0"
    assert planned[1] != "bounded-0"


def test_inflight_automatic_source_yields_safely_and_shared_manual_work_does_not(monkeypatch):
    from iirp.jobs import handlers
    from iirp.jobs.queue import fenced, should_yield_to_manual
    values = CollectionInput.model_validate({"request_id": str(uuid4()), "kind": "sec_history"}).model_dump(mode="json")
    with session() as s, s.begin():
        auto, _ = lifecycle._create(s, values, trigger="automatic", policy_key="sec")
        auto_scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == auto.id))
        a = lifecycle.add_job(s, auto_scope, "sec_discover", {"mode": "fixture-auto"})
        a.checkpoint = {"committed_page": 2}
        automatic_id = a.id
    running = claim({"sec_discover"})
    manual = lifecycle.create_collection({**values, "request_id": str(uuid4())})
    with session() as s, s.begin():
        manual_scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == manual["batch_id"]))
        manual_id = lifecycle.add_job(s, manual_scope, "sec_discover", {"mode": "fixture-manual"}).id
    assert should_yield_to_manual(running)
    from types import SimpleNamespace
    assert not should_yield_to_manual(SimpleNamespace(id="other-lane", kind="market_quote"))
    killed = []
    class Child:
        pid = 123456
        returncode = None
        def poll(self): return self.returncode
        def wait(self, timeout=None):
            self.returncode = 0
            return 0
    monkeypatch.setattr(handlers, "prepare_target", lambda j: j.target)
    monkeypatch.setattr(handlers.subprocess, "Popen", lambda *a, **k: Child())
    monkeypatch.setattr(handlers.os, "killpg", lambda *a: killed.append(a))
    handlers.execute_business(running)
    assert killed, "Stop the external child before releasing its job lease"
    with session() as s:
        a = s.get(Job, automatic_id)
        assert a.status == "QUEUED" and a.attempts == 0 and a.lease_token is None
        assert a.checkpoint["committed_page"] == 2
        assert a.checkpoint["_queue_sec_class"] == "history"
    assert not fenced(running, result={"stale": True}), "Old worker cannot publish after yield"
    foreground = claim({"sec_discover"})
    assert foreground.id == manual_id
    assert claim({"sec_discover"}) is None, "Same-channel automatic work waits while manual runs"
    assert fenced(foreground, status="SUCCEEDED")
    resumed = claim({"sec_discover"})
    assert resumed.id == automatic_id and resumed.checkpoint["committed_page"] == 2
    assert resumed.checkpoint["_queue_sec_class"] == "history"
    with session() as s, s.begin():
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == manual["batch_id"]))
        lifecycle.add_job(s, scope, "sec_discover", resumed.target)
        lifecycle.add_job(s, scope, "sec_discover", {"mode": "another-manual"})
    assert not should_yield_to_manual(resumed), "Shared manual demand is foreground too"
