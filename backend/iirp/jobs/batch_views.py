"""Read-only batch views: per-scope progress, batch lists and their jobs."""

import base64
import json
from datetime import datetime

from sqlalchemy import case, func, select, tuple_

from iirp.db import session
from iirp.market.cache import coverage_for
from iirp.models import (
    ACTIVE,
    AnalysisRequest,
    Batch,
    BatchJob,
    Job,
    RequestScope,
    Security,
    now,
)


def linked_jobs(s, scope_id):
    return s.scalars(
        select(Job).join(BatchJob, BatchJob.job_id == Job.id).where(BatchJob.scope_id == scope_id)
    ).all()


def scope_progress(scope, jobs, *, active_job_ids=None, planning_error=None,
                   planning_retry_at=None, batch_status=None):
    from iirp.jobs.auto_update import STAGES

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
    from iirp.jobs.job_views import job_view

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
