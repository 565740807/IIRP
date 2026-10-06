"""E1 execution boundaries, using isolated PostgreSQL and actual publication fences."""
import copy
import os
import signal
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pytest
from iirp import event_service as service
from iirp import lifecycle
from iirp.business_models import AnalysisRequest, AnalysisResult, Batch, RequestScope
from iirp.business_worker import execute_business
from iirp.db import session
from iirp.event_pipeline import plan_event_compute
from iirp.models import Job, now
from iirp.operation_pool import OperationPool
from iirp.operations import run_request
from iirp.queue import claim, fenced, recover
from sqlalchemy import func, select, text
from test_event_dependencies import revise_dataset
from test_event_service import complete_event_compute, saved
from test_lifecycle import clean_lifecycle, lifecycle_database, seed_prices  # noqa: F401


class LocalCompute:
    def run(self, kind, target, checkpoint):
        assert checkpoint()
        return run_request({"kind": kind, "target": target})


def create(*, prices=True, **params):
    collection = saved()
    security = service.get_set(collection['set_id'])['security']['id']
    dataset = seed_prices(security, date(2024, 5, 1), date(2024, 7, 1)) if prices else None
    result = service.create_analysis(collection['set_id'], {
        'request_id': str(uuid.uuid4()), 'version': 1, 'cutoff_date': '2025-01-01', **params})
    return result, dataset


def enqueue(identifier, capacity=32):
    with session() as s, s.begin():
        request = s.get(AnalysisRequest, identifier)
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == request.batch_id))
        return plan_event_compute(s, request, scope, [capacity])


def results():
    with session() as s:
        return s.scalar(select(func.count()).select_from(AnalysisResult))


def test_create_clone_and_planner_never_compute_and_capacity_waits(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('analysis inside planning transaction')
    monkeypatch.setattr('iirp.analytics.event_dates.analyze_event_dates', forbidden)
    created, _ = create()
    assert service.get_analysis(created['analysis_id'])['data'] is None
    enqueue(created['analysis_id'], 0)
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 0
    lifecycle.plan_tick()
    with session() as s:
        job = s.scalar(select(Job).where(Job.kind == 'event_compute'))
        assert job and job.target['inputs']['event_version_id']
        assert job.target['dependencies']['prices']['cache_id']
    lifecycle.control_batch(created['batch_id'], 'cancel')
    child = lifecycle.control_batch(created['batch_id'], 'continue_remaining')
    lifecycle.plan_tick()
    assert child['batch_id'] != created['batch_id'] and results() == 0


def test_repeated_planning_keeps_leased_input_even_while_row_locked():
    created, dataset = create()
    enqueue(created['analysis_id'])
    job = claim({'event_compute'})
    target = copy.deepcopy(job.target)
    with session() as holder, holder.begin():
        holder.get(Job, job.id, with_for_update=True)
        for _ in range(5):
            enqueue(created['analysis_id'])
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 1
        assert s.get(Job, job.id).target == target
    execute_business(job, runner=LocalCompute())
    assert service.get_analysis(created['analysis_id'])['data']['metadata']['dataset_id'] == dataset
    enqueue(created['analysis_id'])
    assert results() == 1


@pytest.mark.parametrize('change', ['price', 'basis', 'params', 'identity', 'benchmark'])
def test_revision_during_compute_rejects_old_publication(change):
    created, dataset = create()
    enqueue(created['analysis_id'])
    job = claim({'event_compute'})
    class Revision(LocalCompute):
        def run(self, *args):
            response = super().run(*args)
            if change in {'price', 'basis'}:
                revise_dataset(dataset, date(2024, 6, 11), changed_basis=change == 'basis')
            else:
                from iirp.business_models import Security
                with session() as s, s.begin():
                    request = s.get(AnalysisRequest, created['analysis_id'])
                    if change == 'identity':
                        s.get(Security, job.target['security_id']).currency = 'CAD'
                    else:
                        request.params = {**request.params, **({'benchmark': '^IXIC'} if change == 'benchmark' else {'date_window': 'before5'})}
            return response
    execute_business(job, runner=Revision())
    assert results() == 0
    with session() as s:
        assert s.get(Job, job.id).result['result_id'] is None
        assert s.get(Job, job.id).target == job.target
    enqueue(created['analysis_id'])
    complete_event_compute(created['analysis_id'], enqueue=False)
    assert results() == 1


@pytest.mark.parametrize('action', ['pause', 'cancel'])
def test_control_after_compute_before_commit_rejects_response(action):
    created, _ = create()
    enqueue(created['analysis_id'])
    job = claim({'event_compute'})
    class Controlled(LocalCompute):
        def run(self, *args):
            response = super().run(*args)
            lifecycle.control_batch(created['batch_id'], action)
            return response
    execute_business(job, runner=Controlled())
    assert results() == 0
    lifecycle.plan_tick()
    with session() as s:
        assert s.get(Batch, created['batch_id']).status == ('PAUSED' if action == 'pause' else 'CANCELLED')
    assert not fenced(job, status='SUCCEEDED')
    if action == 'pause':
        lifecycle.control_batch(created['batch_id'], 'resume')
        complete_event_compute(created['analysis_id'], enqueue=False)
        assert results() == 1


def test_expired_lease_and_crash_recovery_keep_target_and_reject_old_worker():
    created, _ = create(prices=False)
    enqueue(created['analysis_id'])
    old = claim({'event_compute'})
    with session() as s, s.begin():
        assert s.scalar(text('select current_database()')).startswith('iirp_v1_test_')
        s.get(Job, old.id).lease_until = now() - timedelta(seconds=1)
        s.flush()
        recover(s)
    replacement = claim({'event_compute'})
    assert replacement.target == old.target and replacement.lease_token != old.lease_token
    execute_business(old, runner=LocalCompute())
    assert results() == 0
    execute_business(replacement, runner=LocalCompute())
    assert results() == 1
    partial = service.get_analysis(created['analysis_id'])
    assert partial['data']['rows'] and partial['data']['metadata']['dataset_id'] is None


def test_failure_retry_and_restart_does_not_multiply_jobs():
    created, _ = create()
    enqueue(created['analysis_id'])
    job = claim({'event_compute'})
    class Broken:
        def run(self, *args):
            raise ValueError('synthetic compute failure')
    execute_business(job, runner=Broken())
    for _ in range(3):
        lifecycle.plan_tick()
    with session() as s:
        assert s.get(Batch, created['batch_id']).status == 'PARTIAL'
        assert s.scalar(select(func.count()).select_from(Job)) == 1
    lifecycle.control_batch(created['batch_id'], 'retry_failed')
    complete_event_compute(created['analysis_id'], enqueue=False)
    assert results() == 1


@pytest.mark.parametrize('action', ['pause', 'cancel'])
def test_real_slow_child_does_not_hold_planner_or_control_transaction(action):
    created, _ = create()
    lifecycle.plan_tick()
    job = claim({'event_compute'})
    operations = OperationPool()
    from iirp.operation_pool import OperationChild
    child = OperationChild()
    operations.children['compute'] = child
    child._start()
    proc = child.proc
    os.killpg(proc.pid, signal.SIGSTOP)  # controlled slow CPU operation, no provider
    began = threading.Event()
    class SlowPool:
        def run(self, kind, target, checkpoint):
            def checked():
                began.set()
                return checkpoint()
            return operations.run(kind, target, checked)
    try:
        with ThreadPoolExecutor(max_workers=1) as threads:
            future = threads.submit(execute_business, job, runner=SlowPool())
            assert began.wait(5)
            other = service.create_analysis(service.get_analysis(created['analysis_id'])['params']['event_set_id'],
                {'request_id': str(uuid.uuid4()), 'version': 1, 'cutoff_date': '2025-01-01', 'date_window': 'before5'})
            started = time.perf_counter()
            lifecycle.plan_tick()
            planning = time.perf_counter() - started
            with session() as s:
                request = s.get(AnalysisRequest, other['analysis_id'])
                assert s.scalar(select(Job.id).where(Job.target['analysis_id'].astext == request.id))
            started = time.perf_counter()
            lifecycle.control_batch(created['batch_id'], action)
            control = time.perf_counter() - started
            future.result(timeout=8)
            assert proc.poll() is not None and results() == 0
            assert planning < 2 and control < 2
            print({'slow_child_action': action, 'planning_seconds': planning, 'control_seconds': control})
    finally:
        operations.close()


def test_old_failed_input_does_not_hide_new_success():
    created, dataset = create()
    lifecycle.plan_tick()
    failed = claim({'event_compute'})
    assert fenced(failed, status='FAILED', error='old input failure')
    revise_dataset(dataset, date(2024, 6, 11))
    lifecycle.plan_tick()
    complete_event_compute(created['analysis_id'], enqueue=False)
    lifecycle.plan_tick()
    with session() as s:
        assert s.get(Batch, created['batch_id']).status == 'SUCCEEDED'
        assert s.get(Job, failed.id).status == 'FAILED'


def test_new_review_version_does_not_replace_running_frozen_revision():
    from test_event_contracts import document
    from test_event_service import confirmation, preview
    created, _ = create()
    enqueue(created['analysis_id'])
    job = claim({'event_compute'})
    payload = document()
    payload['events'][0]['event_date'] = '2024-06-11'
    set_id = job.target['params']['event_set_id']
    reviewed = confirmation(preview(payload, set_id=set_id, expected_version=1),
        expected_version=1, revision_note='synthetic reviewed revision')
    reviewed['reviews'][0]['date_verified'] = True
    revised = service.confirm_import(reviewed)
    execute_business(job, runner=LocalCompute())
    result = service.get_analysis(created['analysis_id'])
    assert result['data']['metadata']['event_version_id'] == job.target['inputs']['event_version_id']
    assert result['data']['rows'][0]['anchor']['original_date'] == '2024-06-10'
    newer = service.create_analysis(set_id, {'request_id': str(uuid.uuid4()), 'version': revised['version'], 'cutoff_date': '2025-01-01'})
    complete_event_compute(newer['analysis_id'])
    assert service.get_analysis(newer['analysis_id'])['data']['rows'][0]['anchor']['original_date'] == '2024-06-11'
    assert service.get_analysis(created['analysis_id'], result['result_id'])['data'] == result['data']


def test_killed_worker_is_recovered_with_new_lease(tmp_path):
    import json
    import subprocess
    import sys

    from iirp.config import ROOT

    created, _ = create()
    enqueue(created['analysis_id'])
    marker = tmp_path / 'claimed.json'
    program = '''
import json, sys, time
from pathlib import Path
from iirp.queue import claim
from iirp.business_worker import execute_business
job = claim({'event_compute'})
class CrashPoint:
    def run(self, kind, target, checkpoint):
        assert checkpoint()
        Path(sys.argv[1]).write_text(json.dumps({'id': job.id, 'token': job.lease_token}))
        time.sleep(60)
execute_business(job, runner=CrashPoint())
'''
    process = subprocess.Popen([sys.executable, '-c', program, str(marker)],
        env={**os.environ, 'PYTHONPATH': str(ROOT / 'backend')},
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            assert process.poll() is None
            time.sleep(0.02)
        assert marker.exists()
        before = json.loads(marker.read_text())
        process.kill()
        assert process.wait(timeout=5) == -signal.SIGKILL
        with session() as s, s.begin():
            job = s.get(Job, before['id'])
            assert job.status == 'RUNNING' and job.lease_token == before['token']
            frozen = copy.deepcopy(job.target)
            job.lease_until = now() - timedelta(seconds=1)
        replacement = claim({'event_compute'})
        assert replacement.id == before['id'] and replacement.lease_token != before['token']
        assert replacement.target == frozen
        execute_business(replacement, runner=LocalCompute())
        enqueue(created['analysis_id'])
        assert results() == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stderr.close()


@pytest.mark.parametrize('action', ['pause', 'cancel'])
def test_direct_compute_control_is_visible_and_not_requeued(action):
    from iirp.queue import control
    created, _ = create()
    lifecycle.plan_tick()
    with session() as s:
        job_id = s.scalar(select(Job.id).where(Job.kind == 'event_compute'))
    control(job_id, action)
    lifecycle.plan_tick()
    lifecycle.plan_tick()
    view = service.get_analysis(created['analysis_id'])
    assert view['status'] == 'PARTIAL'
    assert '已暂停或取消' in view['progress'][0]['wait_reason']
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 1


def test_completed_reuse_has_no_compute_but_active_requests_remain_independent():
    created, _ = create()
    params = service.get_analysis(created['analysis_id'])['params']
    second = service.create_analysis(params['event_set_id'], {'request_id': str(uuid.uuid4()),
        'version': 1, 'cutoff_date': '2025-01-01'})
    lifecycle.plan_tick()
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 1  # F shares work; request/result/control identities stay independent.
    complete_event_compute(created['analysis_id'], enqueue=False)
    third = service.create_analysis(params['event_set_id'], {'request_id': str(uuid.uuid4()),
        'version': 1, 'cutoff_date': '2025-01-01'})
    assert service.get_analysis(third['analysis_id'])['data'] == service.get_analysis(created['analysis_id'])['data']
    assert service.get_analysis(second['analysis_id'])['result_id'] != service.get_analysis(third['analysis_id'])['result_id']
    lifecycle.plan_tick()
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 1

    with session() as s:
        owned = s.execute(select(AnalysisResult.analysis_id, AnalysisResult.id)).all()
        assert len({rid for _, rid in owned}) == 3
        assert {aid for aid, _ in owned} == {item['analysis_id'] for item in (created, second, third)}
    assert len({item['batch_id'] for item in (created, second, third)}) == 3


@pytest.mark.parametrize('loss', ['expired', 'replaced'])
def test_lease_loss_after_compute_never_publishes(loss):
    created, _ = create()
    enqueue(created['analysis_id'])
    job = claim({'event_compute'})
    class LostLease(LocalCompute):
        def run(self, *args):
            response = super().run(*args)
            with session() as s, s.begin():
                current = s.get(Job, job.id)
                if loss == 'expired':
                    current.lease_until = now() - timedelta(seconds=1)
                else:
                    current.lease_token = str(uuid.uuid4())
            return response
    execute_business(job, runner=LostLease())
    assert results() == 0


def test_cached_success_resolves_prior_failure_for_same_input():
    created, _ = create()
    lifecycle.plan_tick()
    job = claim({'event_compute'})
    assert fenced(job, status='FAILED', error='synthetic old failure')
    params = service.get_analysis(created['analysis_id'])['params']
    other = service.create_analysis(params['event_set_id'], {'request_id': str(uuid.uuid4()),
        'version': 1, 'cutoff_date': '2025-01-01'})
    complete_event_compute(other['analysis_id'])
    lifecycle.plan_job_scopes(job.id)
    assert service.get_analysis(created['analysis_id'])['status'] == 'SUCCEEDED'
