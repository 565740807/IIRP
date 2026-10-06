"""Batch planning: turn each demand into provider and compute jobs as data arrives."""

import logging
import time
from datetime import timedelta

from sqlalchemy import case, delete, func, select, text
from sqlalchemy.exc import DBAPIError

from iirp.db import session
from iirp.jobs.batch_views import linked_jobs
from iirp.jobs.batches import BUSINESS_KINDS, add_job, defaults
from iirp.market.cache import coverage_for, current_cache
from iirp.market.yahoo import digest
from iirp.models import (
    ACTIVE,
    AnalysisRequest,
    Batch,
    BatchJob,
    BatchPlanSignal,
    Job,
    RequestScope,
    Security,
    now,
)


def _plan_market(s, scope, batch, capacity):
    security = s.get(Security, scope.security_id)
    if security.status == "PENDING":
        job = add_job(
            s, scope, "market_identity", {"symbol": scope.symbol, "security_id": security.id}
        )
        scope.status = "FAILED" if job.status == "FAILED" else "RUNNING"
        scope.wait_reason = job.error
        return
    if batch.kind == "market_quotes":
        target = {
            "symbol": scope.symbol,
            "security_id": security.id,
            "start_date": str(scope.start_date),
            "end_date": str(scope.end_date),
            "round": batch.request_id,
        }
        saved_target = scope.checkpoint.get("quote_target")
        if not saved_target:
            shared = s.scalar(select(Job).where(Job.kind == "market_quote", Job.status.in_(ACTIVE),
                Job.target["security_id"].astext == security.id,
                Job.target["start_date"].astext == str(scope.start_date),
                Job.target["end_date"].astext == str(scope.end_date)).order_by(Job.created_at.desc()).limit(1))
            saved_target = shared.target if shared else target
            scope.checkpoint = {**scope.checkpoint, "quote_target": saved_target}
        job = add_job(s, scope, "market_quote", saved_target)
        scope.status = "READY" if job.status == "SUCCEEDED" else job.status
        scope.wait_reason = job.error
        return
    if security.status != "VERIFIED":
        scope.status, scope.wait_reason = "PARTIAL", "证券身份或交易日历需核对；其他证券继续"
        return
    from iirp.market.cache import ensure_prices, fetch_state

    ensured = ensure_prices(s, scope, security, scope.start_date, scope.end_date)
    status, reason = fetch_state(ensured)
    coverage = coverage_for(s, security, scope.start_date, scope.end_date)
    scope.checkpoint = {**scope.checkpoint, "acquired_sessions": coverage.get("valid_sessions", 0),
                        "expected_sessions": coverage.get("expected_sessions")}
    request = s.scalar(select(AnalysisRequest).where(AnalysisRequest.batch_id == batch.id))
    benchmark_reason = None
    if request and request.params.get("benchmark"):
        from iirp.analysis.benchmarks import plan_benchmark

        benchmark_reason = plan_benchmark(s, scope, request, scope.start_date, scope.end_date)
    scope.status, scope.wait_reason = status, reason
    if status == "READY":
        # Stock results do not wait for a benchmark; a paired result follows.
        benchmark_fetching = any(j.status in ACTIVE and j.kind in ("market_history", "market_identity")
                                 for j in linked_jobs(s, scope.id))
        if benchmark_fetching:
            scope.status = "RUNNING"
        elif benchmark_reason:
            scope.status, scope.wait_reason = "PARTIAL", benchmark_reason
        if _plan_compute(s, scope, batch, security) and not benchmark_fetching:
            scope.status = "PARTIAL" if benchmark_reason else "READY"
            scope.wait_reason = benchmark_reason


def _plan_compute(s, scope, batch, security, *, cache_only=False):
    from iirp.analysis.benchmarks import benchmark_snapshot

    request = s.scalar(select(AnalysisRequest).where(AnalysisRequest.batch_id == batch.id))
    dataset = current_cache(s, security.id)
    if not request or not dataset:
        return
    benchmark = benchmark_snapshot(request.params, security, s)
    input_key = research_input_key(request, dataset, security, benchmark)
    from iirp.analysis.pipeline import coalesce_compute, effective_input_params, reuse_result
    from iirp.analysis.shared_compute import lock_input
    if not lock_input(s, "research_compute", security.id, input_key):
        scope.status, scope.wait_reason = "QUEUED", "等待相同输入共享计算的规划完成"
        return False

    if reuse_result(s, request, security, dataset, benchmark, input_key):
        return True
    if cache_only:
        return False
    job = coalesce_compute(
        s, scope, {
            "analysis_id": request.id,
            "security_id": security.id,
            "dataset_id": dataset.id,
            "input_key": input_key,
            "benchmark": benchmark,
            "params": {**request.params, **effective_input_params(request, security)},
            "calendar": security.calendar or "XNYS",
        },
    )
    if job.status == "SUCCEEDED":
        if reuse_result(s, request, security, dataset, benchmark, input_key):
            return True
        from iirp.analysis.shared_compute import restart_obsolete
        job = restart_obsolete(s, scope, "research_compute", {
            **job.target, "analysis_id": request.id,
        })
    if job.status in ACTIVE:
        scope.status = "RUNNING"
    elif job.status in ("FAILED", "PARTIAL"):
        # Complete price coverage is not a completed research result. Preserve
        # the explicit retry action when this input's computation has failed.
        scope.status, scope.wait_reason = "PARTIAL", job.error


def research_input_key(request, dataset, security, benchmark=None):
    from sqlalchemy.orm import object_session

    from iirp.analysis.benchmarks import benchmark_snapshot
    from iirp.analysis.calendar import calendar_version
    from iirp.analysis.dependencies import benchmark_dependency, dataset_dependency, research_ranges
    from iirp.analysis.pipeline import effective_input_params
    from iirp.analysis.research import CALCULATION_VERSION

    db = object_session(request) or object_session(security)
    effective = effective_input_params(request, security)
    ranges = research_ranges(effective, security.calendar or "XNYS")
    prices = dataset_dependency(db, dataset, ranges) if db else dataset.id
    benchmark = benchmark if benchmark is not None else benchmark_snapshot(request.params, security)
    paired = benchmark_dependency(db, benchmark, ranges) if db else benchmark

    return digest(
        [
            "research-input-f-v1",
            {key: getattr(security, key) for key in
             ("id", "symbol", "name", "currency", "exchange", "status", "calendar")},
            security.instrument,
            effective,
            prices,
            calendar_version(),
            CALCULATION_VERSION,
            paired,
            security.calendar or "XNYS",
        ]
    )


def _planning_due():
    return (Batch.planning_retry_at.is_(None) | (Batch.planning_retry_at <= now())
            | Batch.requested_action.is_not(None))


def _plan_isolated(identifier, statuses, document_link_capacity):
    # Capture control before attempting; a failed old attempt cannot overwrite
    # a concurrent pause/cancel/resume or another planner's successful commit.
    with session() as s:
        observed = s.execute(select(Batch.control_version, Batch.last_planned_at)
                             .where(Batch.id == identifier, _planning_due())).first()
    if observed is None:
        return
    original_capacity = document_link_capacity[:]
    try:
        return _plan_one(identifier, statuses, document_link_capacity)
    except Exception as exc:
        # The transaction rolled back its links; its in-memory reservation must
        # also be returned so later SEC batches retain their turn this tick.
        document_link_capacity[:] = original_capacity
        state = getattr(getattr(exc, "orig", None), "sqlstate", None)
        if isinstance(exc, DBAPIError) and (
            exc.connection_invalidated or state not in {"55P03", "40001", "40P01", "57014", "23514", "23502", "23503", "23505"}
        ):
            raise  # Connection, shutdown, resource and unknown DB errors are global.
        logging.exception("batch planning failed id=%s type=%s sqlstate=%s", identifier, type(exc).__name__, state)
        # This is a separate transaction after the failing write fully rolled back.
        with session() as s, s.begin():
            batch = s.scalar(select(Batch).where(Batch.id == identifier)
                             .with_for_update(skip_locked=True))
            if (batch is None or batch.control_version != observed.control_version
                    or batch.last_planned_at != observed.last_planned_at
                    or batch.status not in statuses or batch.requested_action):
                return
            batch.planning_failures += 1
            stamp = now()
            batch.planning_retry_at = stamp + timedelta(seconds=min(300, 5 * 2 ** min(batch.planning_failures - 1, 6)))
            batch.last_planned_at = stamp
            batch.planning_error = {"type": type(exc).__name__, "sqlstate": state,
                                    "at": stamp.isoformat(), "attempts": batch.planning_failures,
                                    "retry_at": batch.planning_retry_at.isoformat()}
            for scope in s.scalars(select(RequestScope).where(RequestScope.batch_id == identifier)
                                   .with_for_update(skip_locked=True)):
                scope.checkpoint = {**(scope.checkpoint or {}), "planning_conflict": batch.planning_error}


def plan_tick(*, time_budget_seconds=None):
    # Discover identifiers without taking row locks for unrelated work. Each
    # batch is re-read under its own transaction, so durable control can proceed
    # while another research request plans history or computes local results.
    statuses = ("QUEUED", "RUNNING", "RETRY_WAIT", "PAUSE_REQUESTED", "CANCEL_REQUESTED")
    with session() as s, s.begin():
        defaults(s)
        s.execute(delete(BatchPlanSignal).where(BatchPlanSignal.batch_id.in_(
            select(Batch.id).where(Batch.status.in_(("SUCCEEDED", "FAILED", "CANCELLED"))))))
        signaled = s.scalars(select(Batch.id).join(BatchPlanSignal)
            .where(Batch.status.in_((*statuses, "PARTIAL")), _planning_due())
            .order_by(case((Batch.trigger == "manual", 0), else_=1),
                      BatchPlanSignal.updated_at.desc(), Batch.created_at.desc()).limit(8)).all()
        ordering = [
            case((Batch.requested_action.is_not(None), 0), else_=1),
            Batch.last_planned_at.asc().nulls_first(),
            case((Batch.kind.in_(("sec_latest", "market_quotes")), 0), else_=1),
            Batch.created_at.desc(), Batch.id,
        ]
        base = select(Batch.id).where(Batch.status.in_(statuses), _planning_due())
        foreground = s.scalars(
            base.where((Batch.trigger == "manual") | Batch.requested_action.is_not(None))
            .order_by(*ordering).limit(8)
        ).all()
        background = s.scalars(
            base.where(Batch.id.not_in(foreground)).order_by(*ordering).limit(50 - len(foreground))
        ).all()
        # Interleave reserved user/control work with the fair background rotation.
        identifiers = []
        # Fresh dependency completions cannot wait behind thousands of old demands.
        # Keep the first ordinary background turn even under a steady notification stream.
        for i in range(max(len(foreground), len(background))):
            if i < len(foreground):
                identifiers.append(foreground[i])
            if i < len(background):
                identifiers.append(background[i])
        ordinary = identifiers
        guaranteed_turns = max(2, len(set([*signaled[:1], *ordinary[:2]])))
        identifiers = list(dict.fromkeys([*signaled[:1], *ordinary[:2], *signaled[1:], *ordinary[2:]]))
    started = time.monotonic()
    document_link_capacity = [100]
    for position, identifier in enumerate(identifiers):
        # At least one foreground and one background opportunity per turn.
        # A transaction finishes atomically; no mid-write cancellation.
        if time_budget_seconds is not None and position >= guaranteed_turns and time.monotonic() - started >= time_budget_seconds:
            break
        _plan_isolated(identifier, (*statuses, "PARTIAL"), document_link_capacity)


def plan_job_scopes(job_id):
    """A finished dependency should publish the next stage without backlog delay."""
    with session() as s:
        from iirp.jobs.signals import latest_sec_demand
        finished = s.get(Job, job_id)
        newest = latest_sec_demand(s) if finished and finished.kind in ("sec_discover", "sec_document") else None
        linked = select(RequestScope.batch_id).join(BatchJob,
            BatchJob.scope_id == RequestScope.id).where(BatchJob.job_id == job_id,
                BatchJob.active.is_(True))
        identifiers = s.scalars(select(Batch.id).where(Batch.id.in_(linked) | (Batch.id == newest),
            Batch.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT", "PARTIAL")), _planning_due())
            .order_by(case((Batch.trigger == "manual", 0), else_=1),
                      case((Batch.id == newest, 0), else_=1),
                      Batch.last_planned_at.asc().nulls_first(), Batch.created_at.desc())
            .limit(50)).all()
    for identifier in identifiers:
        _plan_isolated(identifier, ("QUEUED", "RUNNING", "RETRY_WAIT", "PARTIAL"), [100])


def _plan_one(identifier, statuses, document_link_capacity):
    with session() as s, s.begin():
        # Recompute global queue capacity after each publication. Competing
        # planners skip this short-lived reservation instead of overbooking
        # it; user controls never acquire this advisory lock.
        if not s.scalar(
            text("SELECT pg_try_advisory_xact_lock(:key)"),
            {"key": int(digest(["planner_capacity"])[:15], 16)},
        ):
            return
        batch = s.scalar(
            select(Batch)
            .where(Batch.id == identifier, Batch.status.in_(statuses), _planning_due())
            .with_for_update(skip_locked=True, key_share=True)
        )
        if batch is None:
            return
        signal = s.get(BatchPlanSignal, identifier)
        observed_token = signal.token if signal else None
        latest_pending = s.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT")),
                Job.kind.in_(BUSINESS_KINDS),
                Job.priority == 0,
            )
        )
        # Latest discovery and documents retain eight queue places across the full chain.
        latest_capacity = [max(0, 8 - latest_pending)]
        capacity = [
            max(
                0,
                32
                - s.scalar(
                    select(func.count())
                    .select_from(Job)
                    .where(
                        Job.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT")),
                        Job.kind.in_(BUSINESS_KINDS),
                    )
                )
                + latest_pending,
            )
        ]
        if batch.kind in ("market_history", "event_dates"):
            # SEC backlog cannot consume Yahoo/compute planning capacity. Shared
            # market jobs still count once across every subscribing research.
            market_kinds = ("market_identity", "market_history", "market_quote", "research_compute")
            market_pending = s.scalar(select(func.count()).select_from(Job).where(
                Job.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT")),
                Job.kind.in_(market_kinds),
            ))
            capacity = [max(0, 32 - market_pending)]
        if batch.trigger == "manual":
            lane_kinds = market_kinds if batch.kind in ("market_history", "event_dates") else (
                "sec_discover", "sec_document", "sec_identity",
            )
            manual_links = (select(BatchJob.job_id)
                .join(RequestScope, RequestScope.id == BatchJob.scope_id)
                .join(Batch, Batch.id == RequestScope.batch_id)
                .where(BatchJob.active.is_(True), Batch.trigger == "manual",
                       Batch.requested_action.is_(None),
                       Batch.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT", "PARTIAL"))))
            manual_pending = s.scalar(select(func.count()).select_from(Job).where(
                Job.id.in_(manual_links), Job.kind.in_(lane_kinds),
                Job.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT")),
            ))
            # A full automatic queue cannot prevent the manual job from even
            # being created. Eight reserved units are shared across manual demand.
            reserve = max(0, 8 - manual_pending)
            capacity[0] = max(capacity[0], reserve)
            latest_capacity[0] = max(latest_capacity[0], reserve)
        _plan_batch(s, batch, capacity, latest_capacity, document_link_capacity)
        batch.last_planned_at = now()
        batch.planning_failures = 0
        batch.planning_error = None
        batch.planning_retry_at = None
        s.flush()
        if observed_token:
            # A concurrently committed newer notification survives this acknowledgement.
            s.execute(delete(BatchPlanSignal).where(BatchPlanSignal.batch_id == identifier,
                                                   BatchPlanSignal.token == observed_token))
        return True


def _plan_batch(s, batch, capacity, latest_capacity, document_link_capacity):
    scopes = s.scalars(select(RequestScope).where(RequestScope.batch_id == batch.id)
                       .order_by(RequestScope.id).with_for_update()).all()
    for scope in scopes:
        if (scope.checkpoint or {}).get("planning_conflict"):
            scope.checkpoint = {
                key: value for key, value in scope.checkpoint.items() if key != "planning_conflict"
            }
    if batch.requested_action:
        pending = any(
            j.lease_token and j.requested_action in ("batch_pause", "batch_cancel")
            for scope in scopes
            for j in linked_jobs(s, scope.id)
        )
        if not pending:
            batch.status = "PAUSED" if batch.requested_action == "pause" else "CANCELLED"
        return
    for scope in scopes:
        if batch.kind == "event_dates":
            from iirp.events.service import plan_event_scope

            plan_event_scope(s, scope, batch, capacity)
        elif scope.security_id:
            _plan_market(s, scope, batch, capacity)
        elif batch.kind == "sec_filing":
            url = batch.params["filing_url"]
            accession = url.rsplit("/", 1)[-1][:-4]
            job = add_job(
                s, scope, "sec_document", {"accession": accession, "submission_url": url}, 1
            )
            scope.status = (
                "READY"
                if job.status == "SUCCEEDED"
                else "RUNNING"
                if job.status in ACTIVE
                else "PARTIAL"
            )
            scope.wait_reason = job.error
        elif batch.policy_key in ("backup", "maintenance"):
            jobs = linked_jobs(s, scope.id)
            scope.status = (
                "RUNNING"
                if any(j.status in ACTIVE for j in jobs)
                else "READY"
                if jobs and all(j.status == "SUCCEEDED" for j in jobs)
                else "PARTIAL"
            )
            scope.wait_reason = next((j.error for j in jobs if j.error), None)
        else:
            from iirp.sec.planning import plan_sec_scope

            plan_sec_scope(
                s,
                scope,
                batch,
                capacity,
                latest_capacity=latest_capacity,
                document_link_capacity=document_link_capacity,
            )
    if scopes and all(x.status == "READY" for x in scopes):
        batch.status = "SUCCEEDED"
    elif scopes and all(x.status in ("READY", "PARTIAL", "FAILED") for x in scopes):
        batch.status = (
            "PARTIAL"
            if any(x.status == "READY" for x in scopes)
            or any(x.status == "PARTIAL" for x in scopes)
            else "FAILED"
        )
    else:
        batch.status = "RUNNING"
    batch.updated_at = now()
