"""Coalesced newest-data requests and read-only source status."""
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select, text

from iirp.business_models import Batch, BatchJob, CollectionStrategy, RequestScope
from iirp.db import session
from iirp.models import ACTIVE, Job, SourceBudget, now

SOURCE_KIND = {"sec": "sec_latest", "market": "market_quotes"}
STAGES = {
    "sec_discover": "检查申报索引", "sec_document": "下载并解析申报",
    "sec_identity": "核对公司身份", "market_identity": "核对证券",
    "market_quote": "获取最新市场报价", "market_history": "补齐历史日线",
    "research_compute": "计算研究结果", "event_compute": "计算事件研究结果", "earnings_evidence": "核对财报公告与财期",
}


def source_status(s, source, batch_id=None):
    policy = s.get(CollectionStrategy, source)
    kind = SOURCE_KIND[source]
    selected = batch_id or (policy.options.get("latest_batch_id") if policy else None)
    batch = s.get(Batch, selected) if selected else None
    if batch is None or batch.kind != kind:
        batch = s.scalar(select(Batch).where(Batch.kind == kind).order_by(Batch.created_at.desc()).limit(1))
    budget = s.get(SourceBudget, "sec" if source == "sec" else "yfinance")
    waiting = budget is not None and budget.next_allowed_at > now() + timedelta(seconds=2)
    checked = s.scalar(select(func.max(Job.finished_at)).where(
        Job.status == "SUCCEEDED",
        Job.kind == ("sec_discover" if source == "sec" else "market_quote"),
        *((Job.target["mode"].astext == "latest",) if source == "sec" else ()),
    ))
    scope_ids = s.scalars(select(RequestScope.id).where(
        RequestScope.batch_id == (batch.id if batch else ""))).all()
    # Materialize the small scope list. The database can then estimate a scope
    # with thousands of links accurately and start from active jobs, not fetch
    # every old successful job via thousands of random heap reads.
    active_links = select(BatchJob.job_id).where(BatchJob.scope_id.in_(scope_ids), BatchJob.active.is_(True))
    active_query = select(Job).where(Job.id.in_(active_links), Job.status.in_(ACTIVE))
    live = ((Job.status == "RUNNING") & Job.lease_token.is_not(None) & (Job.lease_until > now()))
    active_jobs = s.scalars(active_query.order_by(case((live, 0), else_=1),
                                                Job.priority, Job.created_at).limit(20)).all()
    active_count = s.scalar(select(func.count()).select_from(Job).where(
        Job.id.in_(active_links), Job.status.in_(ACTIVE),
    ))
    pending = sorted(active_jobs, key=lambda job: (
        not (job.status == "RUNNING" and job.lease_token and job.lease_until and job.lease_until > now()),
        job.priority, job.created_at,
    ))
    executing = next((job for job in pending if job.status == "RUNNING" and job.lease_token
                      and job.lease_until and job.lease_until > now()), None)
    queued = bool(pending or (batch and batch.status in ("QUEUED", "RUNNING", "RETRY_WAIT")))
    from iirp.business_models import Filing, MarketQuote
    quote_time = None
    if source == "sec":
        latest_date = now().astimezone(ZoneInfo("America/New_York")).date()
        counts = s.execute(select(
            func.count(), func.count(Filing.current_version)
        ).where(Filing.visible.is_(True), Filing.filing_date == latest_date)).one()
        discovered, published = counts
        data_as_of = s.scalar(select(func.max(Filing.accepted_at)).where(Filing.current_version.is_not(None)))
    else:
        discovered = published = s.scalar(select(func.count()).select_from(MarketQuote))
        data_as_of = s.scalar(select(func.max(MarketQuote.data["as_of"].astext)))
        quote_time = s.scalar(select(func.max(MarketQuote.data["source_time"].astext)).where(
            MarketQuote.data["quote_kind"].astext == "provider_snapshot"))
    error = next((job.error for job in pending if job.error), None)
    if not error and batch and batch.status in ("FAILED", "PARTIAL"):
        error = s.scalar(select(Job.error).where(Job.id.in_(active_links),
            Job.status.in_(("FAILED", "PARTIAL")), Job.error.is_not(None))
            .order_by(Job.finished_at.desc()).limit(1))
    paused = not policy or not policy.enabled or (batch and batch.status in ("PAUSED", "PAUSE_REQUESTED"))
    status = (
        "paused" if paused else "checking" if executing else "waiting" if waiting or queued
        else "error" if batch and batch.status in ("FAILED", "PARTIAL")
        else "checked" if checked else "not_checked"
    )
    return {
        "status": status, "enabled": bool(policy and policy.enabled),
        "last_checked_at": checked.isoformat() if checked else None,
        "retry_at": budget.next_allowed_at.isoformat() if waiting else None,
        "batch_id": batch.id if batch else None,
        "stage": "自动更新已暂停" if paused else
            STAGES.get(executing.kind, "处理数据") if executing else
            "等待来源恢复" if waiting else
            ("等待 SEC 通道" if source == "sec" else "等待 Yahoo 通道") if pending else
            "正在安排最新数据检查" if queued else
            "本轮已检查，等待下次检查" if status == "checked" else
            "本轮有缺口，可查看任务重试" if status == "error" else "准备更新",
        "discovered": discovered, "published": published,
        "pending_count": max(0, discovered - published) if source == "sec" else active_count,
        "data_as_of": data_as_of.isoformat() if hasattr(data_as_of, "isoformat") else data_as_of,
        "source_time": quote_time,
        "error": error,
        "progress": [{
            "job_id": j.id, "stage": STAGES.get(j.kind, "处理数据"),
            "status": j.status, "target": {k: v for k, v in j.target.items() if k in (
                "symbol", "accession", "form", "start_date", "end_date", "accepted_at", "filing_date", "mode", "issuer_name"
            )}, "last_progress_at": j.finished_at.isoformat() if j.finished_at else None,
            "retry_at": j.available_at.isoformat() if j.status == "RETRY_WAIT" else None,
        } for j in pending[:5]],
    }


def get_freshness():
    observed = now().isoformat()
    with session() as s:
        return {"sources": {key: source_status(s, key) for key in SOURCE_KIND}, "batch_ids": [],
                "observed_at": observed}



def _advance_latest_round(s, batch):
    from iirp.lifecycle import add_job, digest
    from iirp.sec_facts import _sec_recent_days

    batch = s.scalar(select(Batch).where(
        Batch.id == batch.id, Batch.trigger == "automatic", Batch.requested_action.is_(None),
        Batch.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT")),
    ).with_for_update(skip_locked=True, key_share=True).execution_options(populate_existing=True))
    if batch is None:
        return False
    scopes = list(s.scalars(select(RequestScope).where(RequestScope.batch_id == batch.id)
                           .with_for_update(skip_locked=True)))
    day = now().astimezone(ZoneInfo("America/New_York")).date()
    if not s.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"),
                    {"key": int(digest(["sec_latest_head", str(day)])[:15], 16)}):
        return False
    head = s.scalar(select(Job).where(
        Job.kind == "sec_discover", Job.status.in_(ACTIVE),
        Job.target["mode"].astext == "latest", Job.target["cursor"].astext.is_(None),
        Job.target["end_date"].astext == str(day),
    ).order_by(Job.created_at.desc()).limit(1))
    target = dict(head.target) if head else {
        "mode": "latest", "start_date": str(_sec_recent_days(day, 2)[-1]),
        "end_date": str(day), "max_pages": 1, "round": now().isoformat(),
    }
    if not head:
        complete = s.scalar(select(Job).where(
            Job.kind == "sec_discover", Job.status == "SUCCEEDED",
            Job.target["mode"].astext == "latest",
            Job.checkpoint["sec_scan"]["complete"].astext == "true",
        ).order_by(Job.created_at.desc()).limit(1))
        watermark = (complete.checkpoint or {}).get("sec_scan", {}).get("newest_accepted_at") if complete else None
        if watermark:
            target["watermark"] = watermark
    for scope in scopes:
        scope.checkpoint = {**(scope.checkpoint or {}), "latest_target": target}
        add_job(s, scope, "sec_discover", target, 0)
        scope.status = "RUNNING"
    if scopes:
        batch.status = "RUNNING"
        batch.updated_at = now()
    return bool(scopes)


def _reconcile_market(s, batch):
    """Cheap status repair; only existing quote jobs, never source requests or controls."""
    from iirp.lifecycle import linked_jobs
    from iirp.planning_signals import signal_batch

    signal_batch(s, batch.id)
    current = s.scalar(select(Batch).where(Batch.id == batch.id, Batch.requested_action.is_(None))
                       .with_for_update(skip_locked=True, key_share=True).execution_options(populate_existing=True))
    if current is None:
        return False
    scopes = s.scalars(select(RequestScope).where(RequestScope.batch_id == batch.id)
                      .with_for_update(skip_locked=True)).all()
    expected = s.scalar(select(func.count()).select_from(RequestScope).where(RequestScope.batch_id == batch.id))
    if not scopes or len(scopes) != expected:
        return False
    for scope in scopes:
        jobs = [job for job in linked_jobs(s, scope.id) if job.kind == "market_quote"]
        if not jobs or any(job.status in ACTIVE for job in jobs):
            return False
    for scope in scopes:
        jobs = [job for job in linked_jobs(s, scope.id) if job.kind == "market_quote"]
        scope.status = "READY" if all(job.status == "SUCCEEDED" for job in jobs) else "PARTIAL"
        scope.wait_reason = next((job.error for job in jobs if job.error), None)
    current.status = "SUCCEEDED" if all(scope.status == "READY" for scope in scopes) else "PARTIAL"
    current.updated_at = now()
    return True


def _market_periodic_due(s, policy):
    from iirp.business_models import MarketQuote
    from iirp.market_data import MARKETS

    until = policy.options.get("quote_visible_until")
    if not until or datetime.fromisoformat(until) <= now():
        return []
    quotes = {q.symbol: q for q in s.scalars(select(MarketQuote).where(MarketQuote.symbol.in_(MARKETS)))}
    return [symbol for symbol in MARKETS if symbol not in quotes or not quotes[symbol].data.get("next_refresh_at")
            or datetime.fromisoformat(quotes[symbol].data["next_refresh_at"]) <= now()]


def ensure_fresh_in_session(s, values):
    from iirp.lifecycle import _create, advisory, defaults, digest

    # Routine opens need not contend on INSERT ... ON CONFLICT for every policy.
    if s.get(CollectionStrategy, "sec") is None or s.get(CollectionStrategy, "market") is None:
        defaults(s)
    observed = now().isoformat()
    identifiers = []
    selected_batches = {}
    reason = values.get("reason", "open")
    for source in values.get("sources", list(SOURCE_KIND)):
        market_symbols = None
        if source not in SOURCE_KIND:
            raise ValueError("未知更新来源")
        if values.get("force"):
            advisory(s, ["freshness", source])
        elif not s.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"),
                          {"key": int(digest(["freshness", source])[:15], 16)}):
            # Another opener/scheduler owns this same automatic demand. Its
            # committed state remains readable; do not turn coalescing into 503.
            current = s.get(CollectionStrategy, source)
            if current and current.options.get("latest_batch_id"):
                identifiers.append(current.options["latest_batch_id"])
                selected_batches[source] = current.options["latest_batch_id"]
            continue
        policy = s.scalar(select(CollectionStrategy).where(CollectionStrategy.key == source)
                          .with_for_update(skip_locked=not values.get("force", False)))
        if policy is None:
            continue
        # The old schema increments version on every user toggle. Only untouched
        # initial defaults can adopt the approved policy; restored/user intent stays off.
        if (
            not policy.enabled and policy.version == 1 and not policy.options
            and policy.last_run_at is None and policy.next_run_at is None
            and reason in ("open", "startup")
        ):
            policy.enabled = True
            policy.options = {"default_profile": "latest-first-v1"}
            policy.next_run_at = now()
        if not policy.enabled and not values.get("force", False):
            continue
        if source == "market":
            if values.get("market_visible"):
                policy.options = {**policy.options, "quote_visible_until": (now() + timedelta(seconds=90)).isoformat()}
            if reason in ("scheduler", "startup", "visible") and not values.get("force"):
                market_symbols = _market_periodic_due(s, policy)
                if not market_symbols:
                    continue
        active = s.scalar(select(Batch).where(
            Batch.kind == SOURCE_KIND[source],
            *((Batch.trigger == "manual",) if values.get("force") else ()),
            Batch.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT", "PAUSED", "PAUSE_REQUESTED", "CANCEL_REQUESTED")),
        ).order_by(Batch.created_at.desc()).limit(1))
        current_day = now().astimezone(ZoneInfo("America/New_York")).date()
        older_sec_scope = bool(active and source == "sec" and
                               active.created_at.astimezone(ZoneInfo("America/New_York")).date() < current_day)
        if active:
            selected_batches[source] = active.id
            if active.status in ("PAUSED", "PAUSE_REQUESTED", "CANCEL_REQUESTED"):
                continue
            discovery_pending = s.scalar(select(Job.id).join(BatchJob).join(RequestScope).where(
                RequestScope.batch_id == active.id, Job.status.in_(ACTIVE),
                Job.kind == ("sec_discover" if source == "sec" else "market_quote"),
                *((Job.target["cursor"].astext.is_(None),) if source == "sec" else ()),
            ).limit(1))
            head_planned = s.scalar(select(Job.id).join(BatchJob).join(RequestScope).where(
                RequestScope.batch_id == active.id, Job.kind == "sec_discover",
                Job.target["cursor"].astext.is_(None),
            ).limit(1)) if source == "sec" else True
            if source == "market" and _reconcile_market(s, active):
                active = None
            if active and (source == "market" or (not older_sec_scope and (
                not head_planned or discovery_pending or (now() - active.created_at).total_seconds() < 30
            ))):
                identifiers.append(active.id)
                selected_batches[source] = active.id
                policy.options = {**policy.options, "latest_batch_id": active.id}
                continue
        minimum = 30 if source == "sec" else 60
        stamp = policy.options.get("latest_requested_at")
        if (stamp and not older_sec_scope and not values.get("force")
                and (now() - datetime.fromisoformat(stamp)).total_seconds() < minimum):
            if policy.options.get("latest_batch_id"):
                identifiers.append(policy.options["latest_batch_id"])
                selected_batches[source] = policy.options["latest_batch_id"]
            continue
        if active and source == "sec" and active.trigger == "automatic" and not values.get("force"):
            # Keep same-day discovery and document subscriptions in one durable
            # demand. A new head is independent of old pagination, and all old
            # links remain available to the planner until they finish.
            if not older_sec_scope:
                advanced = _advance_latest_round(s, active)
                identifiers.append(active.id)
                if advanced:
                    policy.options = {**policy.options, "latest_requested_at": now().isoformat(), "latest_batch_id": active.id}
                continue
        batch, _ = _create(s, {
            **({"tickers": market_symbols} if market_symbols else {}),
            "kind": SOURCE_KIND[source], "request_id": "fresh:" + source + ":" + str(uuid4()),
            "purpose": "最新数据检查", "intent": "refresh" if source == "market" else "fetch",
        }, trigger="manual" if values.get("force") else "automatic", policy_key=source)
        if values.get("force") and source == "sec":
            # The manual request is its own visible demand, but subscribing to
            # an in-flight same-day head must not launch another network read.
            from iirp.lifecycle import add_job
            shared = s.scalar(select(Job).where(
                Job.kind == "sec_discover", Job.status.in_(ACTIVE),
                Job.target["mode"].astext == "latest", Job.target["cursor"].astext.is_(None),
                Job.target["end_date"].astext == str(now().astimezone(ZoneInfo("America/New_York")).date()),
            ).order_by(Job.created_at.desc()).limit(1))
            if shared:
                for scope in s.scalars(select(RequestScope).where(RequestScope.batch_id == batch.id)):
                    scope.checkpoint = {**(scope.checkpoint or {}), "latest_target": dict(shared.target)}
                    add_job(s, scope, shared.kind, shared.target, 0)
        identifiers.append(batch.id)
        selected_batches[source] = batch.id
        policy.options = {**policy.options, "latest_requested_at": now().isoformat(), "latest_batch_id": batch.id}
    s.flush()
    return {"sources": ({key: source_status(s, key, selected_batches.get(key)) for key in values.get("sources", SOURCE_KIND)}
                        if reason not in ("scheduler", "startup") else {}),
            "batch_ids": identifiers, "observed_at": observed}


def ensure_fresh(values):
    with session() as s, s.begin():
        return ensure_fresh_in_session(s, values)
