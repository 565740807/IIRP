"""F durable subscriptions: real PG, deterministic race gates and real compute."""
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from iirp import lifecycle
from iirp.api import app
from iirp.business_models import (
    AnalysisRequest,
    AnalysisResult,
    Batch,
    BatchJob,
    RequestScope,
    Security,
    SourceObservation,
)
from iirp.business_worker import execute_business, prepare_target
from iirp.db import session
from iirp.models import Job, now
from iirp.operation_pool import OperationPool
from iirp.operations import operation
from iirp.queue import claim, fenced, recover
from sqlalchemy import func, select
from test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_database,
    seed_prices,
    seed_security,
)
from test_performance_pipeline import params


def setup(mode='monthly'):
    security = seed_security()
    seed_prices(security, date(2022, 1, 1), date(2027, 1, 1))

    def create(**overrides):
        view = lifecycle.create_analysis(params(kind=mode,
            **({'start_mmdd': '01-03', 'end_mmdd': '01-20'} if mode == 'interval' else {}), **overrides))
        return view['id'], view['batch_id']
    return create


def plan(identifier):
    with session() as s, s.begin():
        request = s.get(AnalysisRequest, identifier)
        batch = s.get(Batch, request.batch_id, with_for_update=True)
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch.id).with_for_update())
        return lifecycle._plan_compute(s, scope, batch, s.get(Security, scope.security_id))


def rows():
    with session() as s:
        return s.scalars(select(AnalysisResult).order_by(AnalysisResult.created_at)).all()


def jobs():
    with session() as s:
        return s.scalars(select(Job).where(Job.kind == 'research_compute')).all()


class RealRunner:
    def __init__(self, pool, hook=lambda: None):
        self.pool, self.hook, self.calls = pool, hook, 0

    def run(self, kind, target, checkpoint):
        assert kind == 'research_compute'  # no supplier execution
        self.calls += 1
        self.hook()
        return self.pool.run(kind, target, checkpoint)


@pytest.mark.parametrize('mode', ['monthly', 'interval'])
def test_simultaneous_requests_share_real_compute_and_keep_frozen_ownership(mode):
    create = setup(mode)
    command_gate = threading.Barrier(3)
    def submit(_):
        command_gate.wait()
        return create()
    with ThreadPoolExecutor(3) as threads:
        requests = list(threads.map(submit, range(3)))
    assert len({request[0] for request in requests}) == 3
    gate = threading.Barrier(3)
    def concurrent(request):
        gate.wait()
        plan(request[0])
    with ThreadPoolExecutor(3) as threads:
        list(threads.map(concurrent, requests))
    # A contending planner defers durably instead of consuming lock_timeout.
    # After the concurrent transactions commit, the next pass attaches it.
    for request in requests:
        plan(request[0])
    assert len(jobs()) == 1
    job = claim({'research_compute'})
    # A separate, non-shared numerical execution is the output oracle.
    expected = operation(job.kind, prepare_target(job))
    with closing(OperationPool()) as pool:
        runner = RealRunner(pool)
        execute_business(job, runner=runner)
        assert runner.calls == 1
    published = rows()
    assert len(published) == 3
    assert {r.analysis_id for r in published} == {r[0] for r in requests}
    assert len({r.id for r in published}) == 3
    assert all(r.data['rows'] == published[0].data['rows'] for r in published)
    def numerical(value):
        if isinstance(value, dict):
            return {k: numerical(v) for k, v in value.items()
                    if k not in {'metadata', 'sources', 'review_note'}}
        if isinstance(value, list):
            return [numerical(v) for v in value]
        return value
    assert numerical(published[0].data) == numerical(expected)
    # A fourth independent request uses the completed cache, no lane invocation.
    cached = create()
    plan(cached[0])
    assert len(jobs()) == 1 and len(rows()) == 4
    with session() as s:
        assert s.scalar(select(func.count()).select_from(SourceObservation).where(SourceObservation.provider != 'local')) == 0
        assert s.scalar(select(func.count()).select_from(BatchJob).where(BatchJob.job_id == job.id)) == 3
    with TestClient(app) as client:
        for row in rows():
            url = f'/api/v1/analyses/{row.analysis_id}/export'
            query = {'result_ids': row.id}
            for fmt in ['json', 'csv']:
                response = client.get(url, params={**query, 'format': fmt})
                assert response.status_code == 200 and response.content


@pytest.mark.parametrize('action', ['pause', 'cancel', 'change'])
def test_running_join_and_creator_control_cannot_dominate_other_subscribers(action):
    create = setup()
    first, second = create(), create()
    plan(first[0])
    job = claim({'research_compute'})
    def at_running():
        plan(second[0])
        if action == 'change':
            with session() as s, s.begin():
                request = s.get(AnalysisRequest, first[0])
                request.params = {**request.params, 'month': 2}
        else:
            lifecycle.control_batch(first[1], action)
    with closing(OperationPool()) as pool:
        runner = RealRunner(pool, at_running)
        execute_business(job, runner=runner)
        assert runner.calls == 1
    assert len(jobs()) == 1
    assert [r.analysis_id for r in rows()] == [second[0]]
    if action == 'pause':
        assert lifecycle.get_batch(first[1])['batch']['status'] == 'PAUSED'
        lifecycle.control_batch(first[1], 'resume')
        plan(first[0])
        assert {r.analysis_id for r in rows()} == {first[0], second[0]}
        assert len(jobs()) == 1
    elif action == 'cancel':
        assert lifecycle.get_batch(first[1])['batch']['status'] == 'CANCELLED'


def test_completion_between_cache_check_and_subscription(monkeypatch):
    from iirp import research_pipeline
    create = setup()
    first, second = create(), create()
    plan(first[0])
    job = claim({'research_compute'})
    checked, finished = threading.Event(), threading.Event()
    original = research_pipeline.coalesce_compute
    def gate(*args, **kwargs):
        checked.set()
        assert finished.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(research_pipeline, 'coalesce_compute', gate)
    with ThreadPoolExecutor(1) as threads, closing(OperationPool()) as pool:
        pending = threads.submit(plan, second[0])
        assert checked.wait(5)
        execute_business(job, runner=RealRunner(pool))
        finished.set()
        pending.result(timeout=5)
    assert len(jobs()) == 1 and len(rows()) == 2


def test_all_stopped_resume_and_stale_worker_fence():
    create = setup()
    first, second = create(), create()
    plan(first[0])
    plan(second[0])
    old = claim({'research_compute'})
    for request in [first, second]:
        lifecycle.control_batch(request[1], 'pause')
    assert not fenced(old, acknowledge_control=False)
    assert not fenced(old)
    lifecycle.plan_tick()
    for request in [first, second]:
        lifecycle.control_batch(request[1], 'resume')
    replacement = claim({'research_compute'})
    assert replacement.id == old.id and replacement.lease_token != old.lease_token
    assert not fenced(old, status='SUCCEEDED')
    with closing(OperationPool()) as pool:
        execute_business(replacement, runner=RealRunner(pool))
    assert len(rows()) == 2


def test_shared_crash_recovery_failure_retry_and_duplicate_callback():
    create = setup()
    first, second = create(), create()
    plan(first[0])
    plan(second[0])
    old = claim({'research_compute'})
    with session() as s, s.begin():
        s.get(Job, old.id).lease_until = now() - timedelta(seconds=1)
        s.flush()
        recover(s)
    replacement = claim({'research_compute'})
    assert replacement.id == old.id and replacement.lease_token != old.lease_token
    assert not fenced(old, status='SUCCEEDED')
    assert fenced(replacement, status='FAILED', error='synthetic failure')
    lifecycle.plan_tick()
    lifecycle.control_batch(first[1], 'retry_failed')
    replacement = claim({'research_compute'})
    with closing(OperationPool()) as pool:
        runner = RealRunner(pool)
        execute_business(replacement, runner=runner)
        execute_business(replacement, runner=runner)
        assert runner.calls == 1
    assert len(rows()) == 2 and len(jobs()) == 1


def test_join_after_publication_snapshot_is_served_by_durable_signal(monkeypatch):
    from iirp import shared_compute
    from iirp.business_models import BatchPlanSignal
    create = setup()
    first, second = create(), create()
    plan(first[0])
    job = claim({'research_compute'})
    locked, joined = threading.Event(), threading.Event()
    original = shared_compute.publish
    def gate(s, current, response):
        # fenced has locked only the first subscriber at this point.
        locked.set()
        assert joined.wait(5)
        return original(s, current, response)
    monkeypatch.setattr(shared_compute, 'publish', gate)
    with ThreadPoolExecutor(1) as threads, closing(OperationPool()) as pool:
        pending = threads.submit(execute_business, job, runner=RealRunner(pool))
        assert locked.wait(10)
        plan(second[0])
        joined.set()
        pending.result(timeout=10)
    assert [r.analysis_id for r in rows()] == [first[0]]
    with session() as s:
        assert s.get(BatchPlanSignal, second[1]) is not None
    plan(second[0])
    assert len(rows()) == 2 and len(jobs()) == 1


def test_fanout_rolls_back_all_results_then_retry_keeps_ownership(monkeypatch):
    from iirp import shared_compute
    create = setup()
    first, second = create(), create()
    plan(first[0])
    plan(second[0])
    job = claim({'research_compute'})
    original = shared_compute.publish
    def fail_after_insert(s, current, response):
        result = original(s, current, response)
        assert len(result['subscribers']) == 2
        raise RuntimeError('synthetic fanout rollback')
    monkeypatch.setattr(shared_compute, 'publish', fail_after_insert)
    with closing(OperationPool()) as pool:
        execute_business(job, runner=RealRunner(pool))
        assert rows() == []
        monkeypatch.setattr(shared_compute, 'publish', original)
        lifecycle.plan_tick()
        lifecycle.control_batch(first[1], 'retry_failed')
        execute_business(claim({'research_compute'}), runner=RealRunner(pool))
    assert len(rows()) == 2
    from iirp.maintenance import cleanup
    before = {row.id: row.data for row in rows()}
    cleanup()
    assert {row.id: row.data for row in rows()} == before


def test_label_and_request_time_envelope_are_owned_by_each_native_result():
    create = setup('monthly')
    first, second = create(research_label='First label'), create(research_label='Second label')
    # A later request-time envelope has the same already-frozen mathematical
    # bounds. Only that non-numerical envelope may differ across shared work.
    with session() as s, s.begin():
        request = s.get(AnalysisRequest, second[0])
        request.params = {**request.params, 'price_range': {**request.params['price_range'], 'requested_at': '2026-10-02T12:34:56Z'}}
    plan(first[0])
    plan(second[0])
    with closing(OperationPool()) as pool:
        execute_business(claim({'research_compute'}), runner=RealRunner(pool))
    assert len(jobs()) == 1
    with session() as s:
        for row in rows():
            request = s.get(AnalysisRequest, row.analysis_id)
            assert row.inputs['params'] == request.params
            assert row.data['metadata']['params']['price_range'] == request.params['price_range']
        assert s.get(Batch, first[1]).title.startswith('First label')
        assert s.get(Batch, second[1]).title.startswith('Second label')


def test_completed_new_input_not_held_open_by_other_subscribers_old_work():
    create = setup()
    first, second = create(), create()
    plan(first[0])
    plan(second[0])
    old = jobs()[0]
    with session() as s, s.begin():
        s.get(Job, old.id).available_at = now() + timedelta(hours=1)
        request = s.get(AnalysisRequest, first[0])
        request.params = {**request.params, 'month': 2}
    plan(first[0])
    job = claim({'research_compute'})
    assert job.id != old.id
    with closing(OperationPool()) as pool:
        execute_business(job, runner=RealRunner(pool))
    lifecycle.plan_tick()
    with session() as s:
        assert s.get(Batch, first[1]).status == 'SUCCEEDED'
        assert s.get(Batch, second[1]).status == 'RUNNING'
        assert s.get(Job, old.id).target == old.target
    assert [row.analysis_id for row in rows()] == [first[0]]


def test_retry_old_failure_after_new_request_completed_uses_cache_without_lane():
    create = setup()
    first = create()
    plan(first[0])
    failed = claim({'research_compute'})
    assert fenced(failed, status='FAILED', error='synthetic prior attempt')
    lifecycle.plan_tick()
    assert lifecycle.get_batch(first[1])['batch']['status'] in {'FAILED', 'PARTIAL'}
    second = create()
    plan(second[0])
    with closing(OperationPool()) as pool:
        runner = RealRunner(pool)
        execute_business(claim({'research_compute'}), runner=runner)
        assert [row.analysis_id for row in rows()] == [second[0]]
        lifecycle.control_batch(first[1], 'retry_failed')
        retried = claim({'research_compute'})
        assert retried.id == failed.id
        execute_business(retried, runner=runner)
        assert runner.calls == 1
    assert len(rows()) == 2 and len(jobs()) == 2
    with session() as s:
        assert s.get(Job, failed.id).result['skipped_before_compute']


def test_explicit_retry_and_new_subscriber_share_one_active_generation():
    from iirp.db import engine
    from iirp.market_data import digest
    from iirp.models import ACTIVE
    from sqlalchemy import event, text

    create = setup()
    first = create()
    plan(first[0])
    failed = claim({'research_compute'})
    assert fenced(failed, status='FAILED', error='synthetic admission failure')
    lifecycle.plan_tick()
    second = create()
    control_thread = threading.get_ident()
    pending, seen = [], []
    with ThreadPoolExecutor(1) as threads:
        def after(conn, cursor, statement, parameters, ctx, many):
            if (seen or threading.get_ident() != control_thread
                    or 'job.idempotency_key =' not in statement
                    or 'ORDER BY' in statement or 'SELECT' not in statement):
                return
            seen.append(True)
            # At control's equivalence snapshot, let new admission commit in
            # an unfenced gap. With the fence it must wait for control to
            # commit, then subscribe. No timing/sleep guesses in either case.
            with session() as probe, probe.begin():
                free = probe.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'),
                    {'key': int(digest(['work', failed.idempotency_key])[:15], 16)})
            pending.append(threads.submit(plan, second[0]))
            if free:
                pending[0].result(timeout=5)
        event.listen(engine(), 'after_cursor_execute', after)
        try:
            lifecycle.control_batch(first[1], 'retry_failed')
        finally:
            event.remove(engine(), 'after_cursor_execute', after)
        assert seen
        pending[0].result(timeout=5)
    active = [j for j in jobs() if j.status in ACTIVE]
    assert len(active) == 1
    with session() as s:
        links = s.scalars(select(BatchJob).where(
            BatchJob.job_id == active[0].id, BatchJob.active.is_(True))).all()
        assert len(links) == 2
