"""Durable user batches, shared execution units and local research reads."""

import base64
import csv
import io
import json
import logging
import time
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import case, delete, func, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, OperationalError

from iirp.business_models import (
    AnalysisRequest,
    AnalysisResult,
    Batch,
    BatchJob,
    BatchPlanSignal,
    CollectionStrategy,
    ExportManifest,
    Preferences,
    RequestReceipt,
    RequestScope,
    Security,
)
from iirp.db import session
from iirp.market_data import MARKETS, digest
from iirp.models import ACTIVE, Job, now
from iirp.price_cache import cache_facts, coverage_for, current_cache, price_bars

ET = ZoneInfo("America/New_York")
BUSINESS_KINDS = {
    "market_identity",
    "market_history",
    "market_quote",
    "sec_discover",
    "sec_document",
    "sec_identity",
    "research_compute",
    "maintenance_backup",
    "maintenance_clean",
}


def product_preferences():
    from iirp.profiles import profiles

    normal = profiles()["normal_usage"]
    return {
        "historical_years": normal["price_history_years"],
        "history_months": normal["insider_history_months"],
        "comparison": "same_progress",
        "automatic_history": True,
    }


def resolve_defaults(s, params):
    saved = s.get(Preferences, 1)
    values = {**product_preferences(), **(saved.values if saved else {})}
    return {
        **params,
        "historical_years": params.get("historical_years") or values["historical_years"],
        "history_months": params.get("history_months") or values["history_months"],
    }


def defaults(s):
    for key in ("sec", "market", "backup", "maintenance"):
        s.execute(
            insert(CollectionStrategy)
            .values(
                key=key,
                enabled=key in ("sec", "market"),
                version=1,
                options={"default_profile": "latest-first-v1"},
                next_run_at=now() if key in ("sec", "market") else None,
            )
            .on_conflict_do_nothing()
        )
    s.execute(
        insert(Preferences)
        .values(
            id=1,
            values=product_preferences(),
            version=1,
        )
        .on_conflict_do_nothing()
    )


def advisory(s, value):
    s.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": int(digest(value)[:15], 16)})


def scope_range(params):
    from iirp.analytics.calendar import last_completed_session

    if params.get("price_range"):
        return (date.fromisoformat(params["price_range"]["collection_start_date"]),
                date.fromisoformat(params["price_range"]["collection_end_date"]))
    stamp = now()
    end = last_completed_session(as_of=stamp)
    if params.get("start_date"):
        return date.fromisoformat(params["start_date"]), date.fromisoformat(params["end_date"])
    n = params.get("historical_years") or product_preferences()["historical_years"]
    year = stamp.astimezone(ET).year
    if year - n < 2:
        raise ValueError("历史年数超出日期可表达范围；请选择合法起点")
    if params["kind"] == "sec_latest":
        from iirp.sec_facts import _sec_recent_days

        today = now().astimezone(ET).date()
        # Midnight/weekends retain the latest available prior filing day.
        return _sec_recent_days(today, 2)[-1], today
    if params["kind"] == "sec_filing":
        return now().astimezone(ET).date(), now().astimezone(ET).date()
    if params["kind"] == "sec_history":
        from dateutil.relativedelta import relativedelta

        return now().astimezone(ET).date() - relativedelta(
            months=params.get("history_months") or product_preferences()["history_months"]
        ), now().astimezone(ET).date()
    if params["kind"] == "market_quotes":
        today = stamp.astimezone(ET).date()
        return today - timedelta(days=40), today
    if params["kind"] == "market_history":
        return date(year - n, 1, 1), end
    return date(year - n - 1, 12, 1), end


def _create(s, params, *, trigger="manual", policy_key=None, parent_id=None):
    defaults(s)
    frozen = {k: v for k, v in params.items() if k != "request_id"}
    request_id = params["request_id"]
    key = digest(frozen)
    advisory(s, ["request", request_id])
    receipt = s.get(RequestReceipt, request_id)
    if receipt:
        if receipt.scope_key != key:
            raise ValueError("同一请求标识不能改成不同参数")
        return s.get(Batch, receipt.batch_id), True
    existing = s.scalar(select(Batch).where(Batch.request_id == request_id))
    if existing:
        if existing.scope_key != key:
            raise ValueError("同一请求标识不能改成不同参数")
        return existing, True
    advisory(s, ["scope", key])
    paused = s.scalar(
        select(Batch)
        .where(
            Batch.scope_key == key,
            Batch.status.in_(("PAUSED", "PAUSE_REQUESTED")),
            Batch.trigger == trigger,
        )
        .order_by(Batch.created_at.desc())
    )
    if paused and parent_id is None:
        s.add(RequestReceipt(request_id=request_id, batch_id=paused.id, scope_key=key))
        return paused, True
    params = resolve_defaults(s, params)
    if params["kind"] == "market_history" and not params.get("price_range"):
        from iirp.history_range import price_range
        explicit = ((date.fromisoformat(params["start_date"]), date.fromisoformat(params["end_date"]))
                    if params.get("start_date") else None)
        params = {**params, "price_range": price_range(params, now(), collection=explicit)}
    frozen = {k: v for k, v in params.items() if k != "request_id"}
    start, end = scope_range(params)
    kind = params["kind"]
    symbols = (
        ["SEC"]
        if kind.startswith("sec_")
        else params.get("tickers") or (list(MARKETS) if kind == "market_quotes" else ["SEC"])
    )
    batch = Batch(
        request_id=request_id,
        scope_key=key,
        kind=kind,
        title=(
            "更新最新 Insider"
            if kind == "sec_latest"
            else f"Insider 历史回补 · {start}—{end}"
            if kind == "sec_history"
            else "更新市场行情"
            if kind == "market_quotes"
            else f"{', '.join(symbols[:3])}{' 等' if len(symbols) > 3 else ''}：{params.get('purpose', kind)}"
        ),
        params=frozen,
        trigger=trigger,
        policy_key=policy_key,
        parent_id=parent_id,
    )
    s.add(batch)
    s.flush()
    s.add(RequestReceipt(request_id=request_id, batch_id=batch.id, scope_key=key))
    for symbol in symbols:
        security = None
        if symbol != "SEC":
            advisory(s, ["security", symbol])
            security = s.scalar(
                select(Security).where(Security.symbol == symbol).order_by(Security.id).limit(1)
            )
            if not security:
                security = Security(symbol=symbol)
                s.add(security)
                s.flush()
        s.add(
            RequestScope(
                batch_id=batch.id,
                symbol=symbol,
                security_id=security.id if security else None,
                start_date=start,
                end_date=end,
            )
        )
    s.flush()
    from iirp.planning_signals import signal_batch
    signal_batch(s, batch.id)
    return batch, False


def create_collection(params):
    with session() as s, s.begin():
        if params.get("purpose") == "home_quote_refresh":
            defaults(s)
            policy = s.get(CollectionStrategy, "market", with_for_update=True)
            if not policy.enabled:
                raise ValueError("行情自动更新策略已关闭")
            hold = s.scalar(
                select(Batch)
                .where(
                    Batch.kind == "market_quotes",
                    Batch.status.in_(
                        (
                            "PAUSED",
                            "PAUSE_REQUESTED",
                            "CANCEL_REQUESTED",
                            "QUEUED",
                            "RUNNING",
                            "RETRY_WAIT",
                        )
                    ),
                )
                .limit(1)
            )
            if hold:
                return {"batch_id": hold.id, "reused": True, "batch": batch_view(s, hold)}
            batch, reused = _create(s, params, trigger="automatic", policy_key="market")
        else:
            batch, reused = _create(s, params)
        return {"batch_id": batch.id, "reused": reused, "batch": batch_view(s, batch)}


def create_analysis(params, *, retry_generation=None):
    from iirp.analytics.calendar import last_completed_session
    from iirp.analytics.research import _interval_rules, plan_scope

    submitted = {k: v for k, v in params.items() if k not in {"request_id", "research_label"}}

    def comparable(value):
        return value

    with session() as s:
        receipt = s.get(RequestReceipt, params["request_id"])
        if receipt:
            request = s.scalar(
                select(AnalysisRequest).where(AnalysisRequest.batch_id == receipt.batch_id)
            )
            saved_batch = s.get(Batch, receipt.batch_id)
            if not request or comparable(
                saved_batch.params.get("analysis_input", request.params)
            ) != comparable(submitted):
                raise ValueError("同一分析请求标识不能改变参数")
            return analysis_view(s, request)
    stamp = now()
    completed = last_completed_session(as_of=stamp)
    effective = {**submitted, "cutoff_date": completed.isoformat()}
    as_of_date = stamp.astimezone(ET).date()
    effective["current_year"] = (
        _interval_rules({k: v for k, v in params.items() if v is not None}, as_of_date)[3]
        if params["kind"] == "interval"
        else params.get("current_year") or as_of_date.year
    )
    start, end = plan_scope(effective, today=completed)
    collection = {
        "request_id": params["request_id"],
        "kind": "market_history",
        "tickers": params["tickers"],
        "historical_years": params["historical_years"],
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "purpose": params["kind"],
        "analysis_params": effective,
        "analysis_input": submitted,
        "intent": "fetch",
    }
    if retry_generation:
        collection["retry_generation"] = retry_generation
    from iirp.history_range import price_range
    collection["price_range"] = price_range(collection, stamp, collection=(start, end))
    effective["price_range"] = collection["price_range"]
    with session() as s, s.begin():
        batch, _ = _create(s, collection)
        if params.get("research_label"):
            batch.title = str(params["research_label"]).strip()[:100] + " · " + "、".join(params["tickers"])
        request = s.scalar(select(AnalysisRequest).where(AnalysisRequest.batch_id == batch.id))
        if request is None:
            request = AnalysisRequest(batch_id=batch.id, params=effective)
            s.add(request)
            s.flush()
        identifier = request.id
    # Commit the durable command before cache lookup or response reads. Creation
    # locks protect request/scope/security identity, not result decoding or full
    # coverage serialization for this request's HTTP response.
    _reuse_analysis_cache(identifier)
    return get_analysis(identifier)


def _reuse_analysis_cache(identifier):
    """Optional immediate reuse, fenced like the ordinary durable planner."""
    with session() as s, s.begin():
        request = s.get(AnalysisRequest, identifier)
        if request is None:
            raise LookupError("分析不存在")
        batch = s.scalar(
            select(Batch).where(Batch.id == request.batch_id)
            .with_for_update(skip_locked=True, key_share=True)
        )
        if (batch is None or batch.requested_action
                or batch.status not in ("QUEUED", "RUNNING", "RETRY_WAIT", "PARTIAL")):
            return
        # A planner or control holder can win after command commit. Skip its
        # reservation; the command's durable planning signal remains available.
        scopes = s.scalars(
            select(RequestScope).where(RequestScope.batch_id == batch.id)
            .order_by(RequestScope.id).with_for_update(skip_locked=True)
        ).all()
        for scope in scopes:
            security = s.get(Security, scope.security_id)
            if security and security.status == "VERIFIED":
                _plan_compute(s, scope, batch, security, cache_only=True)


def demand_priority(batch):
    if batch.kind in ("sec_latest", "market_quotes"):
        return 0
    if batch.kind == "sec_history" and not (
        batch.params.get("issuer_id") or batch.params.get("owner_id")
    ):
        return 30
    if batch.policy_key in ("backup", "maintenance"):
        return 50
    return 10 if batch.trigger == "manual" else 30


def add_job(s, scope, kind, target, priority=10, *, reuse_completed=True):
    batch = s.get(Batch, scope.batch_id)
    priority = demand_priority(batch) if batch else priority
    from iirp.shared_compute import work_key
    key = work_key(kind, target)
    advisory(s, ["work", key])
    # A shared planner may have loaded FAILED before waiting behind a retry.
    # Refresh that identity-map entry from the now-serialized DB snapshot.
    job = s.scalar(
        select(Job)
        .where(Job.idempotency_key == key, Job.status.in_(ACTIVE))
        .order_by(Job.created_at.desc())
        .limit(1)
        .execution_options(populate_existing=bool(target.get("shared_compute")))
    )
    if not job and reuse_completed:
        # Completed work is not fetched again just because another view needs it.
        job = s.scalar(
            select(Job).where(Job.idempotency_key == key).order_by(Job.created_at.desc()).limit(1)
            .execution_options(populate_existing=bool(target.get("shared_compute")))
        )
    if (job and batch and (batch.params.get("retry_generation") or target.get("shared_compute"))
            and job.status in ("FAILED", "PARTIAL")
            and not s.get(BatchJob, (scope.id, job.id))):
        # A new explicit/follow-latest demand may retry terminal failed work.
        # Preserve that old task; concurrent demands still share the ACTIVE key.
        # Once this scope has tried the replacement, its ordinary retry cap wins.
        job = None
    if not job:
        job = Job(
            kind=kind,
            title=f"{scope.symbol}：{kind}",
            target=target,
            idempotency_key=key,
            priority=priority,
            progress_total=1,
        )
        s.add(job)
        s.flush()
    job.priority = min(job.priority, priority)
    link = s.get(BatchJob, (scope.id, job.id))
    if not link:
        s.add(BatchJob(scope_id=scope.id, job_id=job.id, active=True))
    else:
        link.active = True
    if job.requested_action in ("batch_pause", "batch_cancel") and job.status in (
        "PAUSED",
        "CANCELLED",
    ):
        job.requested_action = None
        job.status = "QUEUED"
        job.control_version += 1
        job.lease_token = job.lease_until = None
    return job


def linked_jobs(s, scope_id):
    return s.scalars(
        select(Job).join(BatchJob, BatchJob.job_id == Job.id).where(BatchJob.scope_id == scope_id)
    ).all()


def active_demand(s, job_id):
    return bool(
        s.scalar(
            select(func.count())
            .select_from(BatchJob)
            .join(RequestScope, RequestScope.id == BatchJob.scope_id)
            .join(Batch, Batch.id == RequestScope.batch_id)
            .where(
                BatchJob.job_id == job_id,
                BatchJob.active.is_(True),
                Batch.requested_action.is_(None),
                Batch.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT", "PARTIAL")),
            )
        )
    )


def control_batch(batch_id, action):
    # Control and a *single* planner publication must serialize on the same row.
    # Retry only PostgreSQL's rolled-back transient conflicts, with a bounded
    # attempt count and the existing 500 ms lock timeout unchanged. A slow or
    # unavailable database still returns an explicit unconfirmed-operation error.
    for attempt in range(3):
        try:
            return _control_batch_once(batch_id, action)
        except OperationalError as exc:
            if (
                getattr(exc.orig, "sqlstate", None) not in {"55P03", "40001", "40P01"}
                or attempt == 2
            ):
                raise
            time.sleep(0.05 * (attempt + 1))


def _control_batch_once(batch_id, action):
    with session() as s, s.begin():
        batch = s.get(Batch, batch_id, with_for_update=True)
        if not batch:
            raise LookupError("批次不存在")
        scopes = s.scalars(select(RequestScope).where(RequestScope.batch_id == batch.id)).all()
        if action == "continue_remaining":
            if batch.status != "CANCELLED":
                raise ValueError("只有已取消批次才能创建关联续作批次")
            child = s.scalar(
                select(Batch)
                .where(Batch.parent_id == batch.id, Batch.status != "CANCELLED")
                .order_by(Batch.created_at.desc())
                .limit(1)
            )
            if child:
                return {"batch_id": child.id, "reused": True, "batch": batch_view(s, child)}
            if batch.kind == "event_dates":
                from iirp.event_service import clone_event_analysis

                child = clone_event_analysis(s, batch, str(uuid.uuid4()))
                return {"batch_id": child.id, "reused": False, "batch": batch_view(s, child)}
            continued = {**batch.params, "request_id": str(uuid.uuid4())}
            if batch.kind == "market_history" and not continued.get("price_range") and scopes:
                # Legacy batches retain their actually saved scope; never reinterpret
                # a historical request as today's freshly expanded N-year range.
                continued.update(start_date=min(x.start_date for x in scopes).isoformat(),
                                 end_date=max(x.end_date for x in scopes).isoformat())
                from iirp.history_range import price_range
                continued["price_range"] = price_range(continued, batch.created_at, collection=(
                    date.fromisoformat(continued["start_date"]), date.fromisoformat(continued["end_date"])))
                continued["price_range"].update(basis="legacy_scope", explanation="续作原批次已保存范围；旧版未单独记录用户目标与计算缓冲。")
            child, reused = _create(s, continued, parent_id=batch.id)
            original = s.scalar(select(AnalysisRequest).where(AnalysisRequest.batch_id == batch.id))
            if original:
                s.add(AnalysisRequest(batch_id=child.id, params=dict(original.params)))
                s.flush()
            return {"batch_id": child.id, "reused": reused, "batch": batch_view(s, child)}
        if action in ("pause", "cancel"):
            if batch.status in ("SUCCEEDED", "CANCELLED"):
                return {"batch_id": batch.id, "reused": True, "batch": batch_view(s, batch)}
            for scope in scopes:
                if (scope.checkpoint or {}).get("planning_conflict"):
                    scope.checkpoint = {
                        key: value
                        for key, value in scope.checkpoint.items()
                        if key != "planning_conflict"
                    }
            batch.requested_action = action
            batch.status = "PAUSE_REQUESTED" if action == "pause" else "CANCEL_REQUESTED"
            batch.control_version += 1
            exclusive_running = False
            for scope in scopes:
                for job in linked_jobs(s, scope.id):
                    link = s.get(BatchJob, (scope.id, job.id))
                    link.active = False
                    s.flush()
                    if active_demand(s, job.id) or job.status not in ACTIVE:
                        continue
                    job = s.get(Job, job.id, with_for_update=True)
                    job.requested_action = "batch_pause" if action == "pause" else "batch_cancel"
                    job.control_version += 1
                    if job.lease_token:
                        exclusive_running = True
                        job.status = batch.status
                    else:
                        job.status = "PAUSED" if action == "pause" else "CANCELLED"
            if not exclusive_running:
                batch.status = "PAUSED" if action == "pause" else "CANCELLED"
        elif action == "resume":
            if batch.status != "PAUSED":
                raise ValueError("请等待批次实际暂停后再继续")
            batch.status, batch.requested_action = "QUEUED", None
            batch.control_version += 1
            for scope in scopes:
                for job in linked_jobs(s, scope.id):
                    s.get(BatchJob, (scope.id, job.id)).active = True
                    if job.status == "PAUSED" and job.requested_action == "batch_pause":
                        job.status, job.requested_action = "QUEUED", None
                        job.control_version += 1
                        job.lease_token = job.lease_until = None
        elif action == "retry_failed":
            if batch.status not in ("PARTIAL", "FAILED"):
                raise ValueError("只重试失败或部分完成批次")
            batch.status, batch.requested_action = "QUEUED", None
            for scope in scopes:
                scope.status, scope.wait_reason = "QUEUED", None
                for job in linked_jobs(s, scope.id):
                    if job.status in ("FAILED", "PARTIAL"):
                        if (job.kind == "research_compute"
                                and job.target.get("shared_compute") == 1):
                            # Serialize reactivation with a new subscriber's
                            # admission, including the no-active-job snapshot.
                            advisory(s, ["work", job.idempotency_key])
                        equivalent = s.scalar(
                            select(Job).where(
                                Job.idempotency_key == job.idempotency_key, Job.status.in_(ACTIVE)
                            )
                        )
                        if equivalent:
                            add_job(
                                s, scope, equivalent.kind, equivalent.target, equivalent.priority
                            )
                        else:
                            job.status, job.error, job.requested_action = "QUEUED", None, None
                            job.available_at, job.attempts = now(), 0
                            job.control_version += 1
        else:
            raise ValueError("未知批次动作")
        if action in ("pause", "cancel", "resume", "retry_failed"):
            batch.planning_retry_at = None
        batch.updated_at = now()
        s.flush()
        return {"batch_id": batch.id, "reused": False, "batch": batch_view(s, batch)}


def scope_progress(scope, jobs, *, active_job_ids=None, planning_error=None,
                   planning_retry_at=None, batch_status=None):
    from iirp.freshness import STAGES

    stamp = now()

    def executing(job):
        return bool(
            job.status == "RUNNING" and job.lease_token and job.lease_until
            and job.lease_until > stamp
        )

    active = sorted(
        (
            j for j in jobs
            if j.status in ACTIVE and (active_job_ids is None or j.id in active_job_ids)
        ),
        key=lambda j: (not executing(j), j.priority, j.created_at),
    )
    job = active[0] if active else None
    stage = STAGES.get(job.kind, "处理数据") if job else None
    if job and not executing(job):
        source = ("SEC" if job.kind in {"sec_discover", "sec_document", "sec_identity"}
                  else "Yahoo" if job.kind in {"market_identity", "market_history", "market_quote"}
                  else "本地计算")
        prefix = "等待来源重试" if job.status == "RETRY_WAIT" else f"等待 {source} 通道"
        stage = f"{prefix} · {stage.removeprefix('正在')}"
    progress_times = [j.finished_at for j in jobs if j.finished_at and j.status == "SUCCEEDED"]
    checkpoint = scope.checkpoint or {}
    planning_error = planning_error or checkpoint.get("planning_conflict")
    # Diagnostics survive control actions, but only the durable planner schedule
    # authorizes a retry. A shared job's activity belongs to its active demands.
    control_stage = {
        "PAUSE_REQUESTED": "正在暂停此批次；等待执行中的专属任务停止，已完成结果可读",
        "CANCEL_REQUESTED": "正在取消此批次；等待执行中的专属任务停止，已完成结果保留",
        "PAUSED": "此批次已暂停；已完成结果可读，可恢复剩余工作",
        "CANCELLED": "此批次已取消；已完成结果保留",
    }.get(batch_status)
    retry_at = planning_retry_at.isoformat() if planning_retry_at and not control_stage else None
    planning_reason = (
        "批次规划超过数据库执行期限" if planning_error and planning_error.get("sqlstate") == "57014"
        else "批次规划遇到并发更新" if planning_error and planning_error.get("sqlstate") in {"55P03", "40001", "40P01"}
        else "批次规划数据或规则异常" if planning_error
        else None
    )
    activity_status = scope.status
    if scope.status in ("RUNNING", "QUEUED", "WAITING", "RETRY_WAIT"):
        activity_status = (
            "RUNNING" if job and executing(job)
            else job.status if job and job.status != "RUNNING"
            else "WAITING"
        )
    return {
        "activity_status": batch_status if control_stage else activity_status,
        "stage": control_stage or (f"{planning_reason}，稍后自动重试；已完成结果可读"
        if planning_error and retry_at
        else stage
        if job
        else "范围已处理"
        if scope.status == "READY"
        else scope.wait_reason or "等待可执行任务"),
        "current_target": {
            k: v
            for k, v in job.target.items()
            if k
            in (
                "symbol",
                "accession",
                "form",
                "start_date",
                "end_date",
                "filing_date",
                "accepted_at",
            )
        }
        if job and not control_stage
        else {},
        "acquired_sessions": checkpoint.get("acquired_sessions"),
        "expected_sessions": checkpoint.get("expected_sessions"),
        "discovered": checkpoint.get("discovered_count"),
        "parsed": checkpoint.get("parsed_count"),
        "pending": checkpoint.get("unscheduled_documents"),
        "last_progress_at": max(progress_times).isoformat() if progress_times else None,
        "retry_at": None if control_stage else retry_at or (
            job.available_at.isoformat() if job and job.status == "RETRY_WAIT" else None),
        "planning_error": planning_error,
        "history_boundary_reached": checkpoint.get("history_boundary_reached", False),
        "event_count": checkpoint.get("event_count"),
        "ready_events": checkpoint.get("ready_events"),
        "required_ranges": checkpoint.get("required_ranges"),
    }


def _batch_activity_status():
    # RUNNING is the durable planner state, not evidence that a worker is
    # executing this demand. Read active shared links and a live fenced lease.
    executing = (
        select(RequestScope.batch_id)
        .join(BatchJob, BatchJob.scope_id == RequestScope.id)
        .join(Job, Job.id == BatchJob.job_id)
        .where(
            BatchJob.active.is_(True),
            Job.status == "RUNNING",
            Job.lease_token.is_not(None),
            Job.lease_until > func.now(),
        )
    )
    return case(
        ((Batch.status == "RUNNING") & Batch.id.not_in(executing), "WAITING"),
        else_=Batch.status,
    )


def batch_view(s, batch, *, activity_status=None):
    items = []
    for scope in s.scalars(
        select(RequestScope).where(RequestScope.batch_id == batch.id).order_by(RequestScope.symbol)
    ):
        jobs = linked_jobs(s, scope.id)
        security = s.get(Security, scope.security_id) if scope.security_id else None
        coverage = (
            coverage_for(s, security, scope.start_date, scope.end_date)
            if security and batch.kind not in ("market_quotes", "event_dates")
            else {
                "start_date": scope.start_date.isoformat() if scope.start_date else None,
                "end_date": scope.end_date.isoformat() if scope.end_date else None,
                "status": scope.status,
                "reasons": [scope.wait_reason] if scope.wait_reason else [],
            }
        )
        items.append(
            {
                "id": scope.id,
                "symbol": scope.symbol,
                "security_id": scope.security_id,
                "status": scope.status,
                "wait_reason": scope.wait_reason,
                "coverage": coverage,
                "jobs_done": sum(j.status == "SUCCEEDED" for j in jobs),
                "jobs_total": len(jobs),
                "progress": scope_progress(
                    scope, jobs, planning_error=batch.planning_error,
                    planning_retry_at=batch.planning_retry_at, batch_status=batch.status,
                    active_job_ids=set(s.scalars(select(BatchJob.job_id).where(
                        BatchJob.scope_id == scope.id, BatchJob.active.is_(True)
                    ))),
                ),
            }
        )
    return {
        "id": batch.id,
        "analysis_id": s.scalar(
            select(AnalysisRequest.id).where(AnalysisRequest.batch_id == batch.id)
        ),
        "kind": batch.kind,
        "title": batch.title,
        "status": batch.status,
        "activity_status": activity_status
        or (
            s.scalar(select(_batch_activity_status()).where(Batch.id == batch.id))
            if batch.status == "RUNNING"
            else batch.status
        ),
        "params": batch.params,
        "price_range": batch.params.get("price_range"),
        "items": items,
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
        "parent_id": batch.parent_id,
        "requested_action": batch.requested_action,
        "trigger": batch.trigger,
        "policy_key": batch.policy_key,
    }


def list_batches(category="all", cursor="", policy_key="", view="all"):
    from sqlalchemy import case

    states = {
        "active": ["RUNNING", "PAUSE_REQUESTED", "CANCEL_REQUESTED", "QUEUED", "WAITING", "RETRY_WAIT"],
        "running": ["RUNNING", "PAUSE_REQUESTED", "CANCEL_REQUESTED"],
        "waiting": ["QUEUED", "WAITING", "RETRY_WAIT"],
        "attention": ["PAUSED", "FAILED", "PARTIAL"],
        "history": ["SUCCEEDED", "CANCELLED"],
    }
    if view not in {"all", "personal"}:
        raise ValueError("任务视图无效")
    if category not in {"all", *states}:
        raise ValueError("任务分类无效")
    with session() as s:
        activity = _batch_activity_status()
        visible = Batch.trigger == "manual" if view == "personal" else True
        counts = {
            key: s.scalar(select(func.count()).select_from(Batch).where(visible, activity.in_(values)))
            for key, values in states.items() if key != "active"
        }
        key = (
            case(
                ((Batch.trigger == "automatic") & Batch.policy_key.is_not(None), func.concat(
                    Batch.policy_key, ":", Batch.kind, ":", func.coalesce(Batch.params["purpose"].astext, ""),
                    ":", func.coalesce(Batch.params["tickers"].astext, ""),
                    ":", func.coalesce(Batch.params["issuer_id"].astext, ""),
                    ":", func.coalesce(Batch.params["owner_id"].astext, ""))),
                else_=Batch.id,
            )
            if category != "all" and not policy_key
            else Batch.id
        )
        ranking = select(
            Batch.id.label("batch_id"),
            activity.label("activity_status"),
            func.row_number()
            .over(partition_by=key, order_by=[Batch.created_at.desc(), Batch.id.desc()])
            .label("position"),
            func.count().over(partition_by=key).label("history_count"),
        )
        ranking = ranking.where(visible)
        if category != "all":
            ranking = ranking.where(activity.in_(states[category]))
        if policy_key:
            ranking = ranking.where(Batch.policy_key == policy_key)
        ranking = ranking.subquery()
        query = (
            select(Batch, ranking.c.history_count, ranking.c.activity_status)
            .join(ranking, Batch.id == ranking.c.batch_id)
            .where(ranking.c.position == 1)
        )
        if cursor:
            try:
                stamp, identifier = json.loads(base64.urlsafe_b64decode(cursor))
                boundary = datetime.fromisoformat(stamp)
                if boundary.tzinfo is None or not isinstance(identifier, str):
                    raise ValueError
            except (ValueError, TypeError, UnicodeError) as exc:
                raise ValueError("任务分页位置无效") from exc
            query = query.where(tuple_(Batch.created_at, Batch.id) < tuple_(boundary, identifier))
        limit = 50 if category == "all" else 15
        records = s.execute(
            query.order_by(Batch.created_at.desc(), Batch.id.desc()).limit(limit + 1)
        ).all()
        next_cursor = None
        if len(records) > limit:
            last = records[limit - 1][0]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps([last.created_at.isoformat(), last.id]).encode()
            ).decode()
        return {
            "items": [
                {**batch_view(s, batch, activity_status=activity_status), "history_count": count}
                for batch, count, activity_status in records[:limit]
            ],
            "next_cursor": next_cursor,
            "counts": counts,
        }


def get_batch(batch_id):
    with session() as s:
        batch = s.get(Batch, batch_id)
        if not batch:
            raise LookupError("批次不存在")
        return {"batch_id": batch.id, "reused": True, "batch": batch_view(s, batch)}


def batch_jobs(batch_id, cursor="", limit=50):
    from iirp.queue import job_view

    if not 1 <= limit <= 100:
        raise ValueError("每页任务数须在1至100之间")
    with session() as s:
        if not s.get(Batch, batch_id):
            raise LookupError("批次不存在")
        linked = select(BatchJob.job_id).join(RequestScope).where(RequestScope.batch_id == batch_id)
        query = select(Job).where(Job.id.in_(linked))
        if cursor:
            try:
                stamp, identifier = json.loads(base64.urlsafe_b64decode(cursor))
                boundary = datetime.fromisoformat(stamp)
                if boundary.tzinfo is None or not isinstance(identifier, str):
                    raise ValueError
            except (ValueError, TypeError, UnicodeError) as exc:
                raise ValueError("任务分页位置无效") from exc
            query = query.where(tuple_(Job.created_at, Job.id) > tuple_(boundary, identifier))
        rows = s.scalars(query.order_by(Job.created_at, Job.id).limit(limit + 1)).all()
        next_cursor = ""
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps([last.created_at.isoformat(), last.id]).encode()
            ).decode()
        return {
            "items": [job_view(job) for job in rows[:limit]],
            "data": {"batch_id": batch_id, "next_cursor": next_cursor},
        }


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
    from iirp.price_cache import ensure_prices, fetch_state

    ensured = ensure_prices(s, scope, security, scope.start_date, scope.end_date)
    status, reason = fetch_state(ensured)
    coverage = coverage_for(s, security, scope.start_date, scope.end_date)
    scope.checkpoint = {**scope.checkpoint, "acquired_sessions": coverage.get("valid_sessions", 0),
                        "expected_sessions": coverage.get("expected_sessions")}
    request = s.scalar(select(AnalysisRequest).where(AnalysisRequest.batch_id == batch.id))
    benchmark_reason = None
    if request and request.params.get("benchmark"):
        from iirp.benchmarks import plan_benchmark

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
    from iirp.benchmarks import benchmark_snapshot

    request = s.scalar(select(AnalysisRequest).where(AnalysisRequest.batch_id == batch.id))
    dataset = current_cache(s, security.id)
    if not request or not dataset:
        return
    benchmark = benchmark_snapshot(request.params, security, s)
    input_key = research_input_key(request, dataset, security, benchmark)
    from iirp.research_pipeline import coalesce_compute, effective_input_params, reuse_result
    from iirp.shared_compute import lock_input
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
        from iirp.shared_compute import restart_obsolete
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

    from iirp.analytics.calendar import calendar_version
    from iirp.analytics.research import CALCULATION_VERSION
    from iirp.benchmarks import benchmark_snapshot
    from iirp.research_dependencies import benchmark_dependency, dataset_dependency, research_ranges
    from iirp.research_pipeline import effective_input_params

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
        from iirp.planning_signals import latest_sec_demand
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
            from iirp.event_service import plan_event_scope

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
            from iirp.sec_facts import plan_sec_scope

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


def analysis_view(s, request, result_ids=""):
    from iirp.research_freshness import freshness

    # Results live as long as the price caches they used (D14).
    results = s.execute(
        select(AnalysisResult.id, AnalysisResult.security_id, AnalysisResult.input_key,
               AnalysisResult.created_at, AnalysisResult.expires_at)
        .where(AnalysisResult.analysis_id == request.id,
               AnalysisResult.expires_at.is_(None) | (AnalysisResult.expires_at > now()))
        .order_by(AnalysisResult.created_at.desc())
    ).all()
    versions = [
        {"id": r.id, "security_id": r.security_id, "created_at": r.created_at.isoformat()}
        for r in results
    ]
    if result_ids:
        requested = set(result_ids.split(","))
        if not requested.issubset({r.id for r in results}):
            raise LookupError("所选结果已过期或不属于这项研究；请重新获取")
        results = [r for r in results if r.id in requested]
        if len({r.security_id for r in results}) != len(results):
            raise ValueError("每只证券请选择一个结果")
    chosen = {}
    for security_id in dict.fromkeys(r.security_id for r in results):
        security = s.get(Security, security_id)
        # Every unexpired result is current data; show the newest one.
        best = max((r for r in results if r.security_id == security_id),
                   key=lambda r: (r.created_at, r.id))
        result = s.get(AnalysisResult, best.id)
        chosen[security_id] = {
            "symbol": security.symbol,
            "security_id": security.id,
            "result_id": result.id,
            "input_version": result.input_key,
            "created_at": result.created_at,
            "data_published_at": result.inputs.get("price_fetched_at"),
            "expires_at": result.expires_at,
            "is_current": True,
            "coverage": result.inputs.get("coverage"),
            "coverage_basis": "recorded" if result.inputs.get("coverage") else "unknown",
            "result_cutoff": result.inputs.get("params", request.params).get("cutoff_date"),
            "data": result.data,
        }
    batch = s.get(Batch, request.batch_id)
    return {
        "id": request.id,
        "batch_id": batch.id,
        "status": batch.status,
        "params": request.params,
        "results": sorted(
            chosen.values(),
            key=lambda r: request.params.get("tickers", [r["symbol"]]).index(r["symbol"]),
        ),
        "batch": batch_view(s, batch),
        "result_versions": versions,
        "freshness": freshness(s, request, list(chosen.values())),
    }


def recent_analyses(kind="monthly", ticker=""):
    if kind not in {"monthly", "interval"}:
        raise ValueError("请选择月度或区间研究")
    with session() as s:
        query = (
            select(AnalysisRequest, Batch.status)
            .join(Batch, Batch.id == AnalysisRequest.batch_id)
            .where(AnalysisRequest.params["kind"].astext == kind)
        )
        if ticker:
            query = query.where(
                AnalysisRequest.params["tickers"].contains([ticker.strip().upper()])
            )
        return {
            "data": {},
            "items": [
                {
                    "id": request.id,
                    "params": request.params,
                    "created_at": request.created_at.isoformat(),
                    "status": status,
                }
                for request, status in s.execute(
                    query.order_by(
                        AnalysisRequest.created_at.desc(), AnalysisRequest.id.desc()
                    ).limit(24)
                )
            ],
        }


def get_analysis(analysis_id, result_ids=""):
    with session() as s:
        request = s.get(AnalysisRequest, analysis_id)
        if not request:
            raise LookupError("分析不存在")
        return analysis_view(s, request, result_ids)


def export_analysis(analysis_id, result_ids="", format="csv"):
    with session() as s, s.begin():
        from iirp.maintenance import lock_analysis_references

        lock_analysis_references(s)
        request = s.get(AnalysisRequest, analysis_id)
        if not request:
            raise LookupError("分析不存在")
        selected_ids = (
            result_ids.split(",")
            if result_ids
            else [x["result_id"] for x in analysis_view(s, request)["results"]]
        )
        results = s.scalars(
            select(AnalysisResult).where(
                AnalysisResult.analysis_id == analysis_id, AnalysisResult.id.in_(selected_ids)
            )
        ).all()
        if not results or len(results) != len(set(selected_ids)):
            raise ValueError("所选结果已不存在或尚未生成")
        s.add(
            ExportManifest(
                result_ids=selected_ids, params=request.params, expires_at=now() + timedelta(days=7)
            )
        )
        results.sort(key=lambda result: (s.get(Security, result.security_id).symbol, result.id))
        if format == "json":
            frozen = [{
                "symbol": s.get(Security, result.security_id).symbol,
                "security_id": result.security_id, "result_id": result.id,
                "input_version": result.input_key,
                "created_at": result.created_at.isoformat(),
                "params": result.inputs.get("params", request.params),
                "inputs": result.inputs, "data": result.data,
            } for result in results]
            return json.dumps({
                "schema": "iirp.analysis-snapshot.v2", "id": request.id,
                "result_ids": [item["result_id"] for item in frozen],
                "results": frozen,
            }, ensure_ascii=False, indent=2)
        if format != "csv":
            raise ValueError("导出格式须为 csv 或 json")
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(
            [
                "ticker",
                "result_id",
                "input_version",
                "record_type",
                "parameters",
                "input_manifest",
                "data",
            ]
        )
        for result in results:
            security = s.get(Security, result.security_id)
            for record_type, records in (
                ("metadata", [result.data.get("metadata", {})]),
                ("summary", [result.data.get("summary", {})]),
                ("distribution", result.data.get("distributions", [])),
                ("monthly_ranking", result.data.get("monthly_rankings", [])),
                ("benchmark", [result.data["benchmark"]] if result.data.get("benchmark") else []),
                ("row", result.data.get("rows", [])),
                ("cell", result.data.get("cells", [])),
                ("series", result.data.get("series", [])),
            ):
                for row in records:
                    writer.writerow(
                        [
                            security.symbol,
                            result.id,
                            result.input_key,
                            record_type,
                            json.dumps(result.inputs.get("params", request.params), ensure_ascii=False),
                            json.dumps(result.inputs, ensure_ascii=False),
                            json.dumps(row, ensure_ascii=False),
                        ]
                    )
        return "\ufeff" + stream.getvalue()


def get_preferences():
    with session() as s:
        p = s.get(Preferences, 1)
        return {
            "values": p.values if p else product_preferences(),
            "version": p.version if p else 1,
        }


def update_preferences(values):
    with session() as s, s.begin():
        defaults(s)
        p = s.get(Preferences, 1, with_for_update=True)
        previous = {**product_preferences(), **p.values}
        p.values = {**previous, **values}
        p.version += 1
        # Fence old automatic demands when their saved boundary is no longer wanted.
        stop_history = not p.values["automatic_history"] or (
            p.values["history_months"] < previous["history_months"]
        )
        ids = (
            list(
                s.scalars(
                    select(Batch.id).where(
                        Batch.kind == "sec_history",
                        Batch.trigger == "automatic",
                        Batch.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT", "PARTIAL")),
                    )
                )
            )
            if stop_history
            else []
        )
        policy = s.get(CollectionStrategy, "sec", with_for_update=True)
        if policy.enabled:
            policy.next_run_at = now()
        result = {"values": p.values, "version": p.version}
    for identifier in ids:
        control_batch(identifier, "cancel")
    return result


def get_strategies():
    from iirp.providers import SEC_USER_AGENT_HINT, sec_configured

    blocked = {} if sec_configured() else {"sec": SEC_USER_AGENT_HINT}
    with session() as s:
        return {
            "items": [
                {
                    **{
                        k: getattr(p, k)
                        for k in ("key", "enabled", "version", "options", "next_run_at", "last_run_at")
                    },
                    "blocked_reason": blocked.get(p.key),
                }
                for p in s.scalars(select(CollectionStrategy).order_by(CollectionStrategy.key))
            ]
        }


def update_strategy(key, enabled):
    with session() as s, s.begin():
        defaults(s)
        p = s.get(CollectionStrategy, key, with_for_update=True)
        if not p:
            raise LookupError("策略不存在")
        p.enabled = enabled
        p.options = {**p.options, "user_controlled": True}
        p.version += 1
        p.next_run_at = now() if enabled else None
        ids = (
            list(
                s.scalars(
                    select(Batch.id).where(
                        Batch.policy_key == key,
                        Batch.trigger == "automatic",
                        Batch.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT")),
                    )
                )
            )
            if not enabled
            else []
        )
    for batch_id in ids:
        control_batch(batch_id, "pause")
    return get_strategies()


def get_coverage(ticker="", security_id="", start_date="", end_date=""):
    with session() as s:
        from iirp.history_range import price_range
        if bool(start_date) != bool(end_date):
            raise ValueError("起止日期需要一起提供")
        target = price_range(resolve_defaults(s, {"start_date": start_date, "end_date": end_date}), now())
        query = select(Security)
        if ticker:
            query = query.where(Security.symbol == ticker.upper())
        if security_id:
            query = query.where(Security.id == security_id)
        items = []
        for sec in s.scalars(query.order_by(Security.symbol).limit(100)):
            start = date.fromisoformat(target["target_start_date"])
            end = min(date.fromisoformat(target["target_end_date"]), date.fromisoformat(target["completed_through"]))
            items.append(
                {
                    "id": sec.id,
                    "security_id": sec.id,
                    "symbol": sec.symbol,
                    "name": sec.name,
                    "coverage": coverage_for(s, sec, start, end),
                    "price_range": target,
                }
            )
        return {"items": items, "data": {}}


def preview_price_range(historical_years=None, start_date=None, end_date=None, analysis=None):
    """Local planning only: no job, subscription, identity, or result mutation."""
    from iirp.analytics.calendar import last_completed_session
    from iirp.analytics.research import _interval_rules, plan_scope
    from iirp.history_range import price_range
    with session() as s:
        params = resolve_defaults(s, {"historical_years": historical_years,
                                      "start_date": start_date, "end_date": end_date})
        stamp = now()
        bounds = None
        if analysis:
            effective = {k: v for k, v in analysis.items() if v is not None}
            if effective["kind"] == "monthly":
                effective.setdefault("current_year", stamp.astimezone(ET).year)
            else:
                effective["current_year"] = _interval_rules(effective, stamp.astimezone(ET).date())[3]
            params["historical_years"] = effective["historical_years"]
            params["analysis_params"] = effective
            bounds = plan_scope(effective, today=last_completed_session(as_of=stamp))
        return price_range(params, stamp, collection=bounds)


def entity_history(
    kind,
    entity_id,
    start_date,
    end_date,
    recent_count,
    date_basis,
    cursor,
    limit,
    issuer_id="",
    action="all",
):
    from iirp.business_models import FeedSession
    from iirp.entity_reads import read_entity_history

    with session() as s, s.begin():
        # Resolve the first reading's range once. Later pages keep its frozen dates.
        if not start_date and not end_date and not recent_count:
            frozen = s.get(FeedSession, cursor.split(":")[0]) if cursor else None
            if frozen:
                start_date = frozen.filters.get("start")
                end_date = frozen.filters.get("end")
            else:
                values = resolve_defaults(s, {"kind": "sec_history"})
                start, end = scope_range(values)
                start_date, end_date = str(start), str(end)
        return read_entity_history(
            s,
            kind,
            entity_id,
            start_date,
            end_date,
            recent_count,
            date_basis,
            cursor,
            limit,
            issuer_id=issuer_id,
            action=action,
        )


def resolve_amendment(relation_id, action, original_event_id, evidence):
    from iirp import sec_facts

    with session() as s, s.begin():
        return sec_facts.resolve_amendment(s, relation_id, action, original_event_id, evidence)


def transaction_detail(transaction_id, mapping_version=None, cutoff_date=None):
    from iirp import sec_facts
    from iirp.analytics.research import transaction_price_context

    with session() as s:
        record = sec_facts.transaction_record(s, transaction_id)
        from iirp.business_models import TransactionEvent

        event = s.get(TransactionEvent, transaction_id)
        ticker = record.get("ticker")
        if ticker:
            candidates = s.scalars(
                select(Security).where(Security.symbol == ticker, Security.issuer_id.is_(None))
            ).all()
            record["securities"] += [
                {
                    "id": sec.id,
                    "symbol": sec.symbol,
                    "status": sec.status,
                    "issuer_confirmation_required": True,
                }
                for sec in candidates
            ]
        latest_mapping = event.data.get("security_mapping", {})
        history = []
        cursor = latest_mapping
        while isinstance(cursor, dict) and cursor:
            history.append({key: value for key, value in cursor.items() if key != "previous"})
            cursor = cursor.get("previous")
        latest_version = int(latest_mapping.get("version", 1)) if latest_mapping else None
        if mapping_version is not None:
            if not isinstance(mapping_version, int) or mapping_version < 1:
                raise ValueError("证券对应关系版本须为正整数")
            selected = next((item for item in history if int(item.get("version", 1)) == mapping_version), None)
            if selected is None:
                raise LookupError("证券对应关系版本不存在")
            mapping = selected
        else:
            mapping = latest_mapping
        record["security_mapping_history"] = history
        record["mapping_version"] = int(mapping.get("version", 1)) if mapping else None
        record["mapping_latest_version"] = latest_version
        sec = s.get(Security, mapping.get("security_id")) if mapping else None
        context = {"status": "IDENTITY_PENDING", "reason": "交易对应证券尚未核对"}
        if sec and sec.status == "VERIFIED" and sec.issuer_id == event.issuer_id:
            record["security_id"] = sec.id
            record["security_mapping"] = mapping
            bars, cache = price_bars(s, sec.id)
            if record.get("transaction_date") and record.get("accepted_at"):
                observation_date = date.fromisoformat(cutoff_date) if cutoff_date else None
                context = transaction_price_context(
                    bars, record["transaction_date"], record["accepted_at"],
                    today=observation_date, calendar=sec.calendar,
                )
                context["mapping_version"] = record["mapping_version"]
                context["security_id"] = sec.id
                context["source"] = cache.provider if cache else None
                context["price_basis"] = "SPLIT_ONLY" if cache else "UNVERIFIED_PROVIDER_RECORDS"
                context["as_of"] = cache.fetched_at.isoformat() if cache else None
                context.update(cache_facts(cache))

        if record.get("transaction_date") and record.get("accepted_at"):
            accepted = datetime.fromisoformat(record["accepted_at"]).astimezone(ET).date()
            context["disclosure_lag_calendar_days"] = (
                accepted - date.fromisoformat(record["transaction_date"])
            ).days
        return {"items": [], "data": {"transaction": record, "price_context": context}}


def market_detail(symbol):
    from iirp.business_models import MarketQuote

    with session() as s:
        quote = s.get(MarketQuote, symbol)
        security = s.scalar(
            select(Security).where(Security.symbol == symbol).order_by(Security.id).limit(1)
        )
        bars, cache = price_bars(s, security.id) if security else ([], None)
        use_dataset = cache is not None
        records = (
            bars[-60:] if use_dataset else quote.data.get("records", [])[-60:] if quote else []
        )
        data = (
            {**quote.data, "source": quote.data.get("source", "Yahoo Finance / yfinance")}
            if quote
            else {"symbol": symbol, "status": "NOT_FETCHED"}
        )
        if records:
            data.update(
                chart_start=records[0]["date"],
                chart_end=records[-1]["date"],
                chart_source=cache.provider if use_dataset else data.get("source"),
                chart_basis="仅拆股调整，不含分红再投资" if use_dataset else "供应商日线原始口径",
                chart_intraday=False,
                chart_note="日线序列，当日数据可能尚未收盘；不是分时走势" if not use_dataset else "研究日线（24 小时缓存）",
                **(cache_facts(cache) if use_dataset else {}),
            )
        return {
            "items": records,
            "data": data,
        }


def cleanup_cache():
    from iirp.maintenance import _maintenance_batch

    with session() as s, s.begin():
        advisory(s, ["manual", "cache_cleanup"])
        existing = s.scalar(
            select(Batch)
            .where(
                Batch.kind == "maintenance",
                Batch.policy_key == "maintenance",
                Batch.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT", "PAUSED", "PAUSE_REQUESTED")),
            )
            .order_by(Batch.created_at.desc())
            .limit(1)
        )
        if existing is None:
            request_id = "manual-cleanup:" + str(uuid.uuid4())
            _maintenance_batch(s, "maintenance", request_id)
            existing = s.scalar(select(Batch).where(Batch.request_id == request_id))
            existing.trigger = "manual"
        return {
            "items": [],
            "data": {
                "batch_id": existing.id,
                "status": existing.status,
                "message": "已保存缓存清理批次；可在任务中查看和控制",
            },
        }


def map_transaction_security(transaction_id, values):
    from iirp.business_models import SecurityIdentifier, TransactionEvent
    from iirp.sec_facts import transaction_record

    with session() as s, s.begin():
        event = s.get(TransactionEvent, transaction_id, with_for_update=True)
        sec = s.get(Security, values["security_id"], with_for_update=True)
        if not event or not sec:
            raise LookupError("交易或证券不存在")
        if sec.status != "VERIFIED" or sec.issuer_id != event.issuer_id:
            raise ValueError("只能选择已核对身份、且属于同一发行人的具体证券")
        record = transaction_record(s, transaction_id)
        if not event.transaction_date:
            raise ValueError("原申报交易日期未知，不能核对证券有效期")
        if (
            record.get("table") != "I"
            or str(record.get("security_title", "")).strip().casefold() != "common stock"
        ):
            raise ValueError("这条记录并非已明确的普通股；须先核对标的股类或衍生品关系")
        if sec.instrument != "EQUITY" or record.get("ticker") != sec.symbol:
            raise ValueError("申报普通股、证券类别和 ticker 不一致，不能建立对应关系")
        identifier = s.scalar(
            select(SecurityIdentifier).where(
                SecurityIdentifier.security_id == sec.id,
                SecurityIdentifier.symbol == sec.symbol,
                SecurityIdentifier.valid_from <= event.transaction_date,
                (
                    SecurityIdentifier.valid_to.is_(None)
                    | (SecurityIdentifier.valid_to >= event.transaction_date)
                ),
            ).limit(1)
        )
        if identifier is None:
            raise ValueError("交易当日没有有效的证券标识符记录，请先核对历史有效期")
        previous = event.data.get("security_mapping")
        if not (
            previous
            and previous.get("security_id") == sec.id
            and previous.get("evidence") == values["evidence"]
        ):
            event.data = {
                **event.data,
                "security_mapping": {
                    "version": int(previous.get("version", 1)) + 1 if previous else 1,
                    "security_id": sec.id,
                    "symbol": sec.symbol,
                    "evidence": values["evidence"],
                    "identifier_id": identifier.id,
                    "identifier_valid_from": identifier.valid_from.isoformat(),
                    "identifier_valid_to": (
                        identifier.valid_to.isoformat() if identifier.valid_to else None
                    ),
                    "verified_at": now().isoformat(),
                    "previous": previous,
                },
            }
    return transaction_detail(transaction_id)


def transaction_window(transaction_id, request_id):
    from iirp.analytics.calendar import (
        next_regular_open_after,
        next_session,
        reaction_session,
        session_window,
    )
    from iirp.business_models import TransactionEvent

    with session() as s, s.begin():
        event = s.get(TransactionEvent, transaction_id)
        if not event:
            raise LookupError("交易不存在")
        sec = (
            s.get(Security, event.data.get("security_mapping", {}).get("security_id"))
            if event.data.get("security_mapping")
            else None
        )
        if not sec or sec.status != "VERIFIED" or sec.issuer_id != event.issuer_id:
            raise ValueError("先核对具体股类或衍生品标的证券")
        if not event.transaction_date or not event.accepted_at:
            raise ValueError("交易日期或真实 SEC 接受时间缺失，需先补原文")
        receipt = s.get(RequestReceipt, request_id)
        existing = (
            s.get(Batch, receipt.batch_id) if receipt else
            s.scalar(select(Batch).where(Batch.request_id == request_id))
        )
        if existing:
            if (existing.kind != "market_history"
                    or existing.params.get("purpose") != "insider_window"
                    or existing.params.get("transaction_id") != transaction_id):
                raise ValueError("同一请求标识不能改成不同参数")
            # Replay the accepted scope, including legacy conservative buffers.
            # A new request may use today's planner without rewriting user history.
            return {"batch_id": existing.id, "reused": True, "batch": batch_view(s, existing)}
        left = session_window(
            next_session(event.transaction_date, sec.calendar), 5, 5, sec.calendar
        )
        right = session_window(
            date.fromisoformat(
                reaction_session(event.accepted_at, calendar=sec.calendar)["baseline_date"]
            ),
            5,
            5,
            sec.calendar,
        )
        next_open = session_window(
            next_regular_open_after(event.accepted_at, sec.calendar), 0, 4, sec.calendar
        )
        # Intraday disclosure needs the next opening's fifth session; pre-open,
        # after-close and non-session disclosure do not require an extra day.
        end = max(left[-1], right[-1], next_open[-1])
        start = min(left[0], right[0], next_open[0])
        batch, reused = _create(
            s,
            {
                "request_id": request_id,
                "kind": "market_history",
                "tickers": [sec.symbol],
                "start_date": str(start),
                "end_date": str(end),
                "purpose": "insider_window",
                "transaction_id": transaction_id,
            },
        )
        return {"batch_id": batch.id, "reused": reused, "batch": batch_view(s, batch)}


def refresh_analysis(analysis_id, force=False):
    from iirp.research_freshness import refresh_analysis as refresh
    return refresh(analysis_id, force=force)
