"""User demands as batches: creation, job subscription, pause/resume/cancel/retry."""

import time
import uuid
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import OperationalError

from iirp.db import session
from iirp.jobs.batch_views import batch_view, linked_jobs
from iirp.market.yahoo import MARKETS, digest
from iirp.models import (
    ACTIVE,
    AnalysisRequest,
    Batch,
    BatchJob,
    CollectionStrategy,
    Job,
    Preferences,
    RequestReceipt,
    RequestScope,
    Security,
    now,
)

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
    from iirp.jobs.profiles import profiles

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
    from iirp.analysis.calendar import last_completed_session

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
        from iirp.sec.planning import _sec_recent_days

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
        from iirp.analysis.history_range import price_range
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
    from iirp.jobs.signals import signal_batch
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
    from iirp.analysis.shared_compute import work_key
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
                from iirp.events.service import clone_event_analysis

                child = clone_event_analysis(s, batch, str(uuid.uuid4()))
                return {"batch_id": child.id, "reused": False, "batch": batch_view(s, child)}
            continued = {**batch.params, "request_id": str(uuid.uuid4())}
            if batch.kind == "market_history" and not continued.get("price_range") and scopes:
                # Legacy batches retain their actually saved scope; never reinterpret
                # a historical request as today's freshly expanded N-year range.
                continued.update(start_date=min(x.start_date for x in scopes).isoformat(),
                                 end_date=max(x.end_date for x in scopes).isoformat())
                from iirp.analysis.history_range import price_range
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


def cleanup_cache():
    from iirp.jobs.schedule import _maintenance_batch

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
