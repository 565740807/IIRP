"""Persistent at-least-once work with fenced commits and durable user intent."""

import hashlib
import json
import logging
import time
import uuid
from datetime import timedelta

from sqlalchemy import DateTime, case, cast, exists, func, literal_column, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import aliased

from iirp.db import session
from iirp.models import (
    ACTIVE,
    TERMINAL,
    Coverage,
    Job,
    Policy,
    SourceBudget,
    SourceObject,
    Subscription,
    WorkerHeartbeat,
    now,
)
from iirp.profiles import development_budget

SCOPE = (
    "仅限开发测试：小样验证调度。正式使用保留完整新增发现、三个月回补与按需八年行情规则，P2 接入。"
)
TITLES = {
    "fixture_check": "本地任务与存储验证",
    "market_probe": "行情来源小样验证",
    "sec_probe": "SEC 来源小样验证",
}


def job_view(job):
    fields = (
        "id",
        "kind",
        "title",
        "status",
        "trigger",
        "progress_done",
        "progress_total",
        "checkpoint",
        "attempts",
        "error",
        "created_at",
        "updated_at",
        "started_at",
        "available_at",
        "finished_at",
        "requested_action",
        "control_version",
        "result",
    )
    return {
        **{key: getattr(job, key) for key in fields},
        "checkpoint": {key: value for key, value in (job.checkpoint or {}).items()
                       if not key.startswith("_queue_sec_")},
        "control_notice": (job.checkpoint or {}).get("control_notice"),
    }


def policy_view(p):
    return {
        "sec_enabled": p.sec_enabled,
        "version": p.version,
        "updated_at": p.updated_at,
        "next_run_at": p.next_run_at,
        "scope": SCOPE,
    }


def worker_view(s):
    last = s.scalar(select(func.max(WorkerHeartbeat.last_seen)))
    return {"online": bool(last and last > now() - timedelta(seconds=15)), "last_seen": last}


def ensure_defaults():
    with session() as s, s.begin():
        s.execute(
            insert(Policy)
            .values(id=1, sec_enabled=False, version=1, updated_at=now())
            .on_conflict_do_nothing()
        )


def enqueue(s, kind, target, trigger="manual"):
    key = hashlib.sha256(json.dumps([kind, target], sort_keys=True).encode()).hexdigest()
    # The advisory lock also serializes the no-existing-row case.
    s.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": int(key[:15], 16)})
    job = s.scalar(
        select(Job).where(Job.idempotency_key == key, Job.status.in_(ACTIVE)).with_for_update()
    )
    reused = job is not None
    if job is None:
        if s.scalar(select(func.count()).select_from(Job).where(Job.status.in_(ACTIVE))) >= 50:
            raise ValueError("队列已满（50 项），请先处理现有任务。")
        job = Job(
            kind=kind,
            title=TITLES[kind],
            target=target,
            idempotency_key=key,
            trigger=trigger,
            priority=10 if trigger == "manual" else 30,
            progress_total=8 if kind == "fixture_check" else 1,
        )
        s.add(job)
        s.flush()
    # An explicit per-job pause/cancel is never undone by another collection click.
    if job.requested_action not in ("pause", "cancel"):
        sub = s.get(Subscription, (job.id, trigger))
        if sub:
            sub.active = True
        else:
            s.add(Subscription(job_id=job.id, source=trigger))
        if trigger == "manual":
            job.priority = 10
            job.checkpoint = {k: v for k, v in job.checkpoint.items() if k != "control_notice"}
            if job.requested_action == "policy_pause":
                job.control_version += 1
                job.requested_action = None
                job.status = "QUEUED"
                job.lease_token = job.lease_until = None
    s.flush()
    return job, reused


def create_job(kind, target, trigger="manual"):
    # An advisory-lock wait includes earlier contenders, not just one holder.
    # Retry only server-confirmed transaction rejection, after the entire failed
    # transaction has rolled back. Never retry inside enqueue: tick owns its
    # enclosing policy transaction. Connection/commit uncertainty still escapes.
    for attempt in range(3):
        try:
            with session() as s, s.begin():
                return enqueue(s, kind, target, trigger)
        except OperationalError as error:
            state = getattr(error.orig, "sqlstate", None)
            if state not in {"55P03", "40001", "40P01"} or attempt == 2:
                raise
            logging.warning("job creation rolled back kind=%s sqlstate=%s retry=%s", kind, state, attempt + 1)
            time.sleep(0.05 * (attempt + 1))


def control(job_id, action):
    with session() as s, s.begin():
        key = s.scalar(select(Job.idempotency_key).where(Job.id == job_id))
        if key is None:
            raise LookupError("任务不存在。")
        s.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": int(key[:15], 16)})
        job = s.get(Job, job_id, with_for_update=True)
        if action == "pause":
            if job.status in TERMINAL or job.requested_action == "cancel":
                return job
            job.requested_action = "pause"
            job.status = "PAUSE_REQUESTED" if job.lease_token else "PAUSED"
        elif action == "cancel":
            if job.status in TERMINAL:
                return job
            manual = s.get(Subscription, (job.id, "manual"))
            automatic = s.get(Subscription, (job.id, "automatic"))
            if manual is not None:
                manual.active = False
                if automatic and automatic.active:
                    job.checkpoint = {
                        **job.checkpoint,
                        "control_notice": "手动需求已取消；仍有自动需求，公共工作继续。",
                    }
                    job.trigger = "automatic"
                    job.updated_at = now()
                    return job
            if automatic:
                automatic.active = False
            job.requested_action = "cancel"
            job.status = "CANCEL_REQUESTED" if job.lease_token else "CANCELLED"
            if job.status == "CANCELLED":
                job.finished_at = now()
        elif action == "resume":
            if job.status in ("QUEUED", "RUNNING"):
                return job
            if job.status != "PAUSED":
                raise ValueError("请等待任务确认暂停后再恢复。")
            job.requested_action = None
            job.status = "QUEUED"
            job.lease_token = job.lease_until = None
            sub = s.get(Subscription, (job.id, "manual"))
            if sub:
                sub.active = True
            else:
                s.add(Subscription(job_id=job.id, source="manual"))
        elif action == "retry":
            sub = s.get(Subscription, (job.id, "manual"))
            if sub:
                sub.active = True
            else:
                s.add(Subscription(job_id=job.id, source="manual"))
            if job.status in ("QUEUED", "RUNNING", "RETRY_WAIT"):
                return job
            if job.status not in ("FAILED", "PARTIAL"):
                raise ValueError("只有失败或部分完成的任务可以重试。")
            # A newer active equivalent may exist; reuse it instead of violating uniqueness.
            equivalent = s.scalar(
                select(Job).where(
                    Job.idempotency_key == job.idempotency_key, Job.status.in_(ACTIVE)
                )
            )
            if equivalent:
                sub = s.get(Subscription, (equivalent.id, "manual"))
                if sub:
                    sub.active = True
                else:
                    s.add(Subscription(job_id=equivalent.id, source="manual"))
                return equivalent
            job.status, job.requested_action, job.error = "QUEUED", None, None
            job.attempts = 0
            job.available_at, job.finished_at = now(), None
        else:
            raise ValueError("未知控制动作。")
        job.control_version += 1
        job.updated_at = now()
        return job


def update_policy(enabled):
    with session() as s, s.begin():
        p = s.get(Policy, 1, with_for_update=True)
        if p.sec_enabled == enabled:
            return p
        p.sec_enabled, p.updated_at = enabled, now()
        p.version += 1
        p.next_run_at = now() if enabled else None
        jobs = s.scalars(
            select(Job).where(Job.kind == "sec_probe", Job.status.in_(ACTIVE)).with_for_update()
        ).all()
        for job in jobs:
            auto = s.get(Subscription, (job.id, "automatic"))
            if not auto:
                continue
            auto.active = enabled
            manual = s.get(Subscription, (job.id, "manual"))
            if (
                not enabled
                and not (manual and manual.active)
                and job.requested_action not in ("pause", "cancel")
            ):
                job.requested_action = "policy_pause"
                job.control_version += 1
                job.status = "PAUSE_REQUESTED" if job.lease_token else "PAUSED"
            if (
                enabled
                and job.requested_action == "policy_pause"
                and job.status in ("PAUSED", "PAUSE_REQUESTED")
            ):
                job.requested_action, job.status = None, "QUEUED"
                job.lease_token = job.lease_until = None
                job.control_version += 1
        return p


def tick():
    with session() as s, s.begin():
        p = s.get(Policy, 1, with_for_update=True)
        if p.sec_enabled and (p.next_run_at is None or p.next_run_at <= now()):
            try:
                enqueue(s, "sec_probe", {}, "automatic")
            except ValueError:
                p.next_run_at = now() + timedelta(seconds=30)
                return
            p.next_run_at = now() + timedelta(
                minutes=development_budget().scheduled_probe_interval_minutes
            )


def recover(s):
    expired = s.scalars(
        select(Job)
        .where(Job.lease_token.is_not(None), Job.lease_until < now())
        .with_for_update(skip_locked=True)
    ).all()
    from iirp.planning_signals import signal_job

    for job in expired:
        if job.requested_action in ("cancel", "batch_cancel"):
            job.status, job.finished_at = "CANCELLED", now()
        elif job.requested_action in ("pause", "policy_pause", "batch_pause"):
            job.status = "PAUSED"
        else:
            job.status = "QUEUED"
        job.lease_token = job.lease_until = None
        job.updated_at = now()
        signal_job(s, job.id)


def claim(allowed_kinds=None, *, prefer_latest=False):
    with session() as s, s.begin():
        recover(s)
        from iirp.business_models import Batch, BatchJob, Filing, JobDependency, RequestScope

        prerequisite = aliased(Job)
        blocked = exists(
            select(JobDependency.job_id)
            .join(prerequisite, prerequisite.id == JobDependency.prerequisite_id)
            .where(JobDependency.job_id == Job.id, prerequisite.status != "SUCCEEDED")
        )
        # Shared cooldown is waiting, not another attempt for every filing.
        sec_blocked = exists(select(SourceBudget.provider).where(
            SourceBudget.provider == "sec", SourceBudget.next_allowed_at > now() + timedelta(seconds=2)
        ))
        yahoo_blocked = exists(select(SourceBudget.provider).where(
            SourceBudget.provider == "yfinance", SourceBudget.next_allowed_at > now()
        ))
        query = select(Job).where(
            ~blocked,
            ~(Job.kind.in_(("sec_discover", "sec_document", "sec_identity", "earnings_evidence")) & sec_blocked),
            ~(Job.kind.in_(("market_identity", "market_history", "market_quote", "earnings_candidates")) & yahoo_blocked),
        )
        # While manual work owns a provider lane, automatic work waits even if
        # that lane has another slot. Other provider lanes remain independent.
        manual_links = (select(BatchJob.job_id)
            .join(RequestScope, RequestScope.id == BatchJob.scope_id)
            .join(Batch, Batch.id == RequestScope.batch_id)
            .where(BatchJob.active.is_(True), Batch.trigger == "manual",
                   Batch.requested_action.is_(None),
                   Batch.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT", "PARTIAL"))))
        for lane in SOURCE_LANES:
            if allowed_kinds is not None and not set(lane).intersection(allowed_kinds):
                continue
            demand = aliased(Job)
            manual_waiting = exists(select(demand.id).where(
                demand.kind.in_(lane), demand.id.in_(manual_links),
                demand.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT")),
                demand.requested_action.is_(None), demand.available_at <= now(),
            ))
            query = query.where(~Job.kind.in_(lane) | Job.id.in_(manual_links) | ~manual_waiting)
        # Only active subscriptions determine priority; pause removes that demand.
        tier = func.coalesce(
            select(func.min(case(
                ((Batch.trigger == "manual") & Batch.kind.in_(("sec_latest", "market_quotes")), -20),
                (Batch.trigger == "manual", -10),
                (Batch.kind.in_(("sec_latest", "market_quotes")), 0),
                ((Batch.kind == "sec_history") & Batch.params["issuer_id"].astext.is_(None) & Batch.params["owner_id"].astext.is_(None), 30),
                (Batch.policy_key.in_(("backup", "maintenance")), 50),
                (Batch.trigger == "manual", 10), else_=30,
            )))
            .select_from(BatchJob)
            .join(RequestScope, RequestScope.id == BatchJob.scope_id)
            .join(Batch, Batch.id == RequestScope.batch_id)
            .where(BatchJob.job_id == Job.id, BatchJob.active.is_(True),
                   Batch.requested_action.is_(None),
                   Batch.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT", "PARTIAL")))
            .correlate(Job).scalar_subquery(),
            Job.priority,
        )
        filing_recency = (
            select(func.coalesce(Filing.accepted_at, cast(Filing.filing_date, DateTime(timezone=True))))
            .where(Filing.accession == Job.target["accession"].astext)
            .correlate(Job).scalar_subquery()
        )
        sec_preferred = None
        if prefer_latest:
            running = set(s.scalars(select(Job.kind).where(
                Job.kind.in_(("sec_discover", "sec_document")), ~Job.id.in_(manual_links),
                Job.status == "RUNNING", Job.lease_token.is_not(None), Job.lease_until > now())))
            if "sec_discover" in running and "sec_document" not in running:
                sec_preferred = "sec_document"
            elif "sec_document" in running:
                sec_preferred = "sec_discover"
            else:
                last_kind = s.scalar(select(Job.kind).where(
                    Job.kind.in_(("sec_discover", "sec_document")), ~Job.id.in_(manual_links),
                    Job.finished_at.is_not(None))
                    .order_by(Job.finished_at.desc()).limit(1))
                sec_preferred = "sec_document" if last_kind == "sec_discover" else "sec_discover"
        if allowed_kinds is not None:
            query = query.where(Job.kind.in_(allowed_kinds))
        # A preferred lane with no claimable work cannot set the history turn:
        # otherwise an endless latest discovery stream can hide quarterly scans.
        if sec_preferred and s.scalar(
            query.with_only_columns(Job.id)
            .where(Job.kind == sec_preferred,
                   Job.status.in_(("QUEUED", "RETRY_WAIT")),
                   Job.requested_action.is_(None), Job.available_at <= now())
            .limit(1)
        ) is None:
            sec_preferred = ("sec_document" if sec_preferred == "sec_discover"
                             else "sec_discover")
        # Alternate latest and aged history within the preferred SEC kind.
        # A bounded planner can enqueue the next historical document only after
        # earlier ones finish. Requiring each new wave to wait an hour leaves a
        # full document queue idle behind a continuous latest discovery stream.
        history_wait = timedelta(minutes=2)
        # The tier alone would starve old quarterly scans and documents while
        # latest work keeps arriving; filing recency would starve old documents
        # even after lifting their tier. Manual demand retains its negative tier.
        effective_tier = tier
        history_first = Job.priority * 0 + 1
        if sec_preferred:
            # Claim-time class is stored with the job. Current batch links can
            # change later, so they cannot classify an earlier shared claim.
            # Keep the stamp key literal so PostgreSQL can use the partial
            # expression index under prepared/generic plans. This is a fixed
            # internal key, never derived from request data.
            claimed_stamp = literal_column("job.checkpoint ->> '_queue_sec_claimed_at'")
            last_served = s.scalar(
                select(Job.checkpoint["_queue_sec_class"].astext)
                .where(Job.kind == sec_preferred,
                       Job.checkpoint["_queue_sec_class"].astext.in_(("latest", "history")),
                       claimed_stamp.is_not(None))
                .order_by(claimed_stamp.desc())
                .limit(1)
            )
            # If there is no claimable latest job of this kind, an empty
            # latest turn must not let another kind's tier-0 work starve it.
            latest_links = (
                select(BatchJob.job_id)
                .join(RequestScope, RequestScope.id == BatchJob.scope_id)
                .join(Batch, Batch.id == RequestScope.batch_id)
                .where(BatchJob.active.is_(True), Batch.trigger == "automatic",
                       Batch.kind == "sec_latest", Batch.requested_action.is_(None),
                       Batch.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT", "PARTIAL")))
            )
            latest_of_kind = s.scalar(
                query.with_only_columns(Job.id)
                .where(Job.kind == sec_preferred, Job.id.in_(latest_links),
                       Job.status.in_(("QUEUED", "RETRY_WAIT")),
                       Job.requested_action.is_(None), Job.available_at <= now())
                .limit(1)
            )
            if last_served == "latest" or latest_of_kind is None:
                aged_history = ((Job.kind == sec_preferred) & (tier >= 30)
                                & (Job.created_at <= now() - history_wait))
                effective_tier = case((aged_history, 0), else_=tier)
                history_first = case((aged_history, 0), else_=1)
        job = s.scalar(
            query.where(
                Job.status.in_(("QUEUED", "RETRY_WAIT")),
                Job.requested_action.is_(None),
                Job.available_at <= now(),
            )
            .order_by(
                effective_tier,
                history_first,
                # On a history turn, take the oldest waiting job before filing
                # recency; otherwise fresh filings could starve old ones.
                case((history_first == 0, Job.created_at), else_=None).asc().nulls_last(),
                # Bounded SEC lanes give discovery and document publication turns.
                # Manual/shared-manual tier still wins before this preference.
                case((Job.kind == sec_preferred, 0), else_=1) if sec_preferred else Job.priority * 0,
                case(((Job.kind == "sec_discover") & (Job.target["mode"].astext == "latest")
                      & Job.target["cursor"].astext.is_(None), 0), else_=1),
                case((Job.kind == "sec_document", filing_recency), else_=None).desc().nulls_last(),
                case(((Job.kind == "sec_discover") & (Job.target["mode"].astext == "latest")
                      & Job.target["cursor"].astext.is_(None), Job.target["end_date"].astext), else_=None).desc().nulls_last(),
                case(((Job.kind == "sec_discover") & (Job.target["mode"].astext == "latest")
                      & Job.target["cursor"].astext.is_(None), Job.created_at), else_=None).desc().nulls_last(),
                Job.priority - func.floor(func.extract("epoch", now() - Job.created_at) / 60),
                Job.created_at,
            )
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return None
        if job.kind in ("sec_discover", "sec_document"):
            demand = s.execute(
                select(Batch.trigger, Batch.kind)
                .select_from(BatchJob)
                .join(RequestScope, RequestScope.id == BatchJob.scope_id)
                .join(Batch, Batch.id == RequestScope.batch_id)
                .where(BatchJob.job_id == job.id, BatchJob.active.is_(True),
                       Batch.requested_action.is_(None),
                       Batch.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT", "PARTIAL")))
                .order_by(case((Batch.trigger == "manual", 0),
                               (Batch.kind == "sec_latest", 1), else_=2))
                .limit(1)
            ).first()
            demand_class = ("manual" if demand and demand.trigger == "manual" else
                            "latest" if demand and demand.kind == "sec_latest" else
                            "history" if demand and demand.kind == "sec_history" else "other")
            job.checkpoint = {**(job.checkpoint or {}),
                              "_queue_sec_class": demand_class,
                              "_queue_sec_claimed_at": now().isoformat()}
        job.status, job.lease_token = "RUNNING", str(uuid.uuid4())
        job.lease_until = now() + timedelta(seconds=30)
        job.heartbeat_at = job.updated_at = now()
        job.started_at = job.started_at or now()
        job.attempts += 1
        s.flush()
        return job



# Provider lanes, not all background work, yield to a manual request.
SOURCE_LANES = (
    ("sec_discover", "sec_document", "sec_identity", "earnings_evidence"),
    ("market_identity", "market_history", "market_quote", "earnings_candidates"),
)


class ManualPriorityYield(Exception):
    """A source child must stop before its automatic job is returned to the queue."""


def should_yield_to_manual(job):
    from iirp.business_models import Batch, BatchJob, RequestScope

    kinds = next((lane for lane in SOURCE_LANES if job.kind in lane), None)
    if kinds is None:
        return False
    with session() as s:
        manual = (
            select(Job.id).join(BatchJob, BatchJob.job_id == Job.id)
            .join(RequestScope, RequestScope.id == BatchJob.scope_id)
            .join(Batch, Batch.id == RequestScope.batch_id)
            .where(BatchJob.active.is_(True), Batch.trigger == "manual",
                   Batch.requested_action.is_(None),
                   Batch.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT", "PARTIAL")))
        )
        # A shared download serving manual demand is already foreground work.
        if s.scalar(manual.where(Job.id == job.id).limit(1)):
            return False
        return s.scalar(manual.where(
            Job.kind.in_(kinds), Job.id != job.id,
            Job.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT")),
            Job.requested_action.is_(None), Job.available_at <= now(),
        ).limit(1)) is not None


def fenced(
    job,
    *,
    done=None,
    checkpoint=None,
    result=None,
    source=None,
    coverage=None,
    status=None,
    error=None,
    retry_seconds=None,
    acknowledge_control=True,
    business_write=None,
):
    """Every visible result and file reference commits under the same lease+control fence."""
    with session() as s, s.begin():
        if business_write is not None and job.kind in {"research_compute", "event_compute"}:
            from iirp.shared_compute import lock_subscribers
            lock_subscribers(s, job.id)
        if business_write is not None:
            from iirp.business_models import BatchJob, RequestScope
            # Planners and completions both lock scope -> job. Reading a stale
            # JSON checkpoint then replacing it could otherwise lose a cursor.
            list(s.scalars(select(RequestScope).join(BatchJob, BatchJob.scope_id == RequestScope.id)
                .where(BatchJob.job_id == job.id, BatchJob.active.is_(True))
                .order_by(RequestScope.id).with_for_update(of=RequestScope)))
        current = s.get(Job, job.id, with_for_update=(
            {"key_share": True} if job.kind in {"research_compute", "event_compute"} else True
        ))
        if (
            not current
            or current.lease_token != job.lease_token
            or not current.lease_until
            or current.lease_until <= now()
        ):
            return False
        if current.requested_action:
            if not acknowledge_control:
                return False
            current.status = (
                "CANCELLED" if current.requested_action in ("cancel", "batch_cancel") else "PAUSED"
            )
            current.lease_token = current.lease_until = None
            current.updated_at = now()
            if current.status == "CANCELLED":
                current.finished_at = now()
            from iirp.planning_signals import signal_job
            signal_job(s, current.id)
            return False
        if current.control_version != job.control_version:
            return False
        current.heartbeat_at = current.updated_at = now()
        current.lease_until = now() + timedelta(seconds=30)
        if source:
            s.execute(insert(SourceObject).values(**source).on_conflict_do_nothing())
        if business_write is not None:
            business_write(s, current)
        if coverage:
            s.execute(
                insert(Coverage)
                .values(**coverage)
                .on_conflict_do_update(
                    index_elements=[Coverage.provider, Coverage.target], set_=coverage
                )
            )
        if done is not None:
            current.progress_done = done
        if checkpoint is not None:
            # Worker progress may replace its checkpoint; retain scheduler
            # history needed to rotate shared SEC demands on later claims.
            queue_stamp = {key: value for key, value in (current.checkpoint or {}).items()
                           if key in ("_queue_sec_class", "_queue_sec_claimed_at")}
            current.checkpoint = {**checkpoint, **queue_stamp}
        if result is not None:
            current.result = result
        if status:
            current.status, current.error = status, error
            current.lease_token = current.lease_until = None
            if status in TERMINAL:
                current.finished_at = now()
            if retry_seconds is not None:
                current.available_at = now() + timedelta(seconds=retry_seconds)
        if current.lease_token is None:
            from iirp.planning_signals import signal_job
            signal_job(s, current.id)
        return True


def heartbeat(worker_id):
    with session() as s, s.begin():
        s.execute(
            insert(WorkerHeartbeat)
            .values(id=worker_id, last_seen=now())
            .on_conflict_do_update(index_elements=[WorkerHeartbeat.id], set_={"last_seen": now()})
        )
