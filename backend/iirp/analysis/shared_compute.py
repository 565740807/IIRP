"""Durable cross-request compute admission; BatchJob remains the subscription.

Inputs on Job never depend on a creator's mutable request. Batch/scope locks
fence subscribers at publication; the ordinary job lease fences the worker.
"""
from sqlalchemy import func, select, text

from iirp.market.yahoo import digest
from iirp.messages import msg
from iirp.models import (
    ACTIVE,
    AnalysisRequest,
    AnalysisResult,
    Batch,
    BatchJob,
    Job,
    RequestScope,
    now,
)

COMPUTE_KINDS = {'research_compute'}


def work_key(kind, target):
    if kind in COMPUTE_KINDS and target.get('shared_compute') == 1:
        return digest(['shared-compute-v1', kind, target['security_id'], target['input_key']])
    return digest([kind, target])


def lock_input(s, kind, security_id, input_key):
    return s.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'),
        {'key': int(digest(['shared-compute-v1', kind, security_id, input_key])[:15], 16)})


def enqueue(s, scope, kind, target, capacity=None):
    from iirp.jobs.batches import add_job
    target = {**target, 'shared_compute': 1}
    key = work_key(kind, target)
    # Caller holds the input lock across cache lookup and admission. Never skip
    # a leased row merely because a heartbeat currently owns its row lock.
    matching = s.scalar(select(Job).where(Job.idempotency_key == key)
        .order_by(Job.status.in_(ACTIVE).desc(), Job.created_at.desc()).limit(1))
    if matching and matching.status == 'SUCCEEDED':
        # Completion may have committed since the caller's cache read. Return
        # it so the caller rechecks the cache before starting a new generation.
        return matching
    if not matching or matching.status not in ACTIVE:
        if capacity is not None and capacity[0] <= 0:
            return None
        # Preserve request-local coalescing only when nobody else ever owned
        # this unleased work. Shared or running targets are immutable.
        old = s.scalar(select(Job).join(BatchJob, BatchJob.job_id == Job.id).where(
            BatchJob.scope_id == scope.id, Job.kind == kind,
            Job.status.in_(('QUEUED', 'RETRY_WAIT')), Job.lease_token.is_(None),
            Job.requested_action.is_(None), Job.idempotency_key != key,
        ).order_by(Job.created_at.desc()).with_for_update(of=Job, skip_locked=True).limit(1))
        old_locked = bool(old and s.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'),
            {'key': int(digest(['shared-compute-v1', kind, old.target['security_id'], old.target['input_key']])[:15], 16)}))
        # Try, don't wait while holding a different input lock: A->B and B->A
        # planners must not deadlock. The old input lock excludes a concurrent
        # subscriber whose uncommitted link would be invisible to COUNT.
        if old_locked and s.scalar(select(func.count()).select_from(BatchJob).where(BatchJob.job_id == old.id)) == 1:
            old.checkpoint = {**old.checkpoint, 'replaced_input': old.target['input_key'],
                'merged_versions': old.checkpoint.get('merged_versions', 0) + 1}
            old.target, old.idempotency_key = target, key
            old.status, old.available_at = 'QUEUED', now()
            return old
    job = add_job(s, scope, kind, target, 5)
    if not matching and capacity is not None:
        capacity[0] -= 1
    return job


def restart_obsolete(s, scope, kind, target, capacity=None):
    """A finished work item with no compatible result (e.g. A -> B -> A)."""
    from iirp.jobs.batches import add_job
    if capacity is not None and capacity[0] <= 0:
        return None
    job = add_job(s, scope, kind, {**target, 'shared_compute': 1}, 5, reuse_completed=False)
    if capacity is not None:
        capacity[0] -= 1
    return job


def lock_subscribers(s, job_id):
    # Match control_batch/planner's batch -> scope -> job order. The snapshot
    # bounds this publication: a late join not locked here is served by the
    # durable completion signal/cache on its next planning pass.
    batches = list(s.scalars(select(Batch).join(RequestScope, RequestScope.batch_id == Batch.id)
        .join(BatchJob, BatchJob.scope_id == RequestScope.id)
        .where(BatchJob.job_id == job_id, BatchJob.active.is_(True))
        .order_by(Batch.id).with_for_update(of=Batch)))
    ids = [b.id for b in batches if not b.requested_action
           and b.status in ('QUEUED', 'RUNNING', 'RETRY_WAIT', 'PARTIAL')]
    s.info['compute_batch_ids'] = ids


def subscribers(s, job):
    ids = s.info.get('compute_batch_ids', [])
    return s.execute(select(AnalysisRequest, RequestScope)
        .join(RequestScope, RequestScope.batch_id == AnalysisRequest.batch_id)
        .join(BatchJob, BatchJob.scope_id == RequestScope.id)
        .where(BatchJob.job_id == job.id, BatchJob.active.is_(True),
               RequestScope.batch_id.in_(ids), RequestScope.security_id == job.target['security_id'])
        .order_by(RequestScope.id)).all()


def pending_subscribers(s, job):
    from iirp.jobs.planner import research_input_key
    from iirp.market.cache import current_cache
    from iirp.models import Security
    security = s.get(Security, job.target['security_id'])
    for request, scope in subscribers(s, job):
        dataset = current_cache(s, security.id)
        compatible = bool(dataset and research_input_key(request, dataset, security) == job.target['input_key'])
        cached = s.scalar(select(AnalysisResult.id).where(AnalysisResult.analysis_id == request.id,
            AnalysisResult.security_id == scope.security_id, AnalysisResult.input_key == job.target['input_key']))
        if compatible and not cached:
            yield request, scope


def publish(s, job, response):
    """One fenced transaction, distinct immutable result IDs for each subscriber."""
    from iirp.storage.maintenance import lock_analysis_references
    lock_analysis_references(s)
    published = []
    for request, scope in subscribers(s, job):
        row = publish_native(s, request, scope, job.target, response)
        if row:
            published.append({'analysis_id': request.id, 'result_id': row.id})
            # Flush each payload separately; don't accumulate a full payload
            # copy per subscriber in the Session's pending INSERT collection.
            s.flush()
            s.expunge(row)
    return {'message': msg('compute.published' if published else 'compute.superseded'),
            'result_id': published[0]['result_id'] if published else None,
            'subscribers': published}


def result_expiry(s, target):
    """A result lives as long as the earliest of its price caches (D14)."""
    from datetime import timedelta

    from iirp.market.cache import CACHE_HOURS
    from iirp.models import PriceCache
    ids = [target.get('dataset_id'), (target.get('benchmark') or {}).get('dataset_id')]
    stamps = [cache.expires_at if (cache := s.get(PriceCache, i)) else now() for i in ids if i]
    return min(stamps, default=now() + timedelta(hours=CACHE_HOURS))


def publish_native(s, request, scope, target, response):
    from iirp.analysis.pipeline import frozen_coverage, owned_result_data, safe_publication
    from iirp.analysis.research import CALCULATION_VERSION
    from iirp.analysis.result_reuse import result_identity
    from iirp.market.cache import current_cache
    from iirp.models import PriceCache, Security
    security = s.get(Security, scope.security_id)
    if not safe_publication(s, request, security, target, current_cache(s, security.id)):
        return None
    existing = result_identity(s, request.id, security.id, target['input_key'])
    if existing:
        return existing
    original = s.get(PriceCache, target['dataset_id'])
    metadata = {'source': original.provider, 'dataset_id': original.id,
        'price_fetched_at': original.fetched_at.isoformat(),
        'price_expires_at': original.expires_at.isoformat(), 'price_basis': 'SPLIT_ONLY'}
    data = {**response, 'metadata': {**response.get('metadata', {}), **metadata,
        'params': {**response.get('metadata', {}).get('params', {}), **request.params},
        'calculation_version': CALCULATION_VERSION}}
    row = AnalysisResult(analysis_id=request.id, security_id=security.id, input_key=target['input_key'],
        inputs={'dataset_id': original.id, 'calculation_version': CALCULATION_VERSION,
            'params': request.params, 'source': msg('market.source_cache'),
            'price_fetched_at': original.fetched_at.isoformat(),
            'benchmark': target.get('benchmark'), 'coverage': frozen_coverage(s, request, security, original),
            'dependencies': response.get('metadata', {}).get('dependencies')},
        data=owned_result_data(data, request.params), expires_at=result_expiry(s, target))
    s.add(row)
    s.flush()
    return row


def reuse_pending(s, job):
    """An explicitly retried old job can outlive a newer completed equivalent.

    Resolve its subscribers from that completed result under this job's fence,
    before opening the numerical lane. A per-request cache check alone misses
    this legitimate failed-A / completed-B / retry-A sequence.
    """
    source_id = s.scalar(select(AnalysisResult.id).where(
        AnalysisResult.security_id == job.target['security_id'],
        AnalysisResult.input_key == job.target['input_key'],
        AnalysisResult.expires_at > now()).limit(1))
    if source_id is None:
        return
    from iirp.analysis.pipeline import reuse_result
    from iirp.market.cache import current_cache
    from iirp.models import Security
    from iirp.storage.maintenance import lock_analysis_references
    lock_analysis_references(s)
    for request, scope in pending_subscribers(s, job):
        security = s.get(Security, scope.security_id)
        row = reuse_result(s, request, security, current_cache(s, security.id),
                           job.target.get('benchmark'), job.target['input_key'])
        if row:
            s.flush()
            s.expunge(row)
