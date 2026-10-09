"""Automatic schedules: SEC polling cadence and daily backup/maintenance demands."""

import uuid
from datetime import timedelta

from sqlalchemy import select

from iirp.analysis.calendar import ET, sessions
from iirp.config import refresh
from iirp.db import session
from iirp.jobs.providers import sec_configured
from iirp.messages import msg
from iirp.models import (
    Batch,
    CollectionStrategy,
    Job,
    RequestReceipt,
    RequestScope,
    Subscription,
    now,
)


def sec_workday(day):
    from pandas.tseries.holiday import USFederalHolidayCalendar

    return day.weekday() < 5 and not len(USFederalHolidayCalendar().holidays(start=day, end=day))


def sec_poll_seconds(stamp):
    cadence = refresh()["sec"]
    current = stamp.astimezone(ET)
    if not sec_workday(current.date()):
        return cadence["non_business_seconds"]
    minute = current.hour * 60 + current.minute
    if not 360 <= minute < 1320:
        return cadence["night_seconds"]
    if not sessions(current.date(), current.date()) or minute < 570:
        return cadence["premarket_seconds"]
    return cadence["session_seconds"]


def _has_running(s, key, kind=None):
    query = select(Batch.id).where(
        Batch.policy_key == key,
        Batch.requested_action.is_(None),
        Batch.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT")),
    )
    if kind:
        query = query.where(Batch.kind == kind)
    return s.scalar(query.limit(1)) is not None


def _maintenance_batch(s, policy, request_id):
    from iirp.jobs.batches import add_job
    from iirp.market.yahoo import digest

    params = {
        "kind": "maintenance",
        "purpose": msg("maintenance.purpose.backup" if policy == "backup" else "maintenance.purpose.clean"),
    }
    key = digest(params)
    batch = Batch(
        request_id=request_id,
        scope_key=key,
        kind="maintenance",
        title=params["purpose"],
        params=params,
        trigger="automatic",
        policy_key=policy,
    )
    s.add(batch)
    s.flush()
    s.add(RequestReceipt(request_id=request_id, batch_id=batch.id, scope_key=key))
    scope = RequestScope(batch_id=batch.id, symbol="maintenance")
    s.add(scope)
    s.flush()
    add_job(
        s,
        scope,
        "maintenance_backup" if policy == "backup" else "maintenance_clean",
        {"round": request_id},
        50,
    )


def _sec_schedule(s, policy, current, request_id):
    from datetime import date

    from iirp.jobs.auto_update import ensure_fresh_in_session
    from iirp.jobs.batches import _create, product_preferences, scope_range
    from iirp.models import Preferences
    from iirp.sec.planning import _sec_recent_days

    ensure_fresh_in_session(s, {"sources": ["sec"], "reason": "scheduler"})
    saved = s.get(Preferences, 1)
    preferences = {**product_preferences(), **(saved.values if saved else {})}
    options = dict(policy.options)
    policy.next_run_at = current + timedelta(seconds=sec_poll_seconds(current))
    # A scheduled range is not a completed range. Advance the frontier only after
    # its whole manifest and documents have committed; failed/paused work stays visible.
    pending_id = options.get("history_pending_batch")
    pending = s.get(Batch, pending_id) if pending_id else None
    if pending and pending.status == "SUCCEEDED":
        start, end = date.fromisoformat(pending.params["start_date"]), date.fromisoformat(pending.params["end_date"])
        old_start, old_end = options.get("history_start"), options.get("history_end")
        options["history_start"] = str(min(start, date.fromisoformat(old_start))) if old_start else str(start)
        options["history_end"] = str(max(end, date.fromisoformat(old_end))) if old_end else str(end)
        if pending.params.get("intent") == "refresh":
            options["sec_weekly"] = pending.created_at.astimezone(ET).strftime("%G-W%V")
        options.pop("history_pending_batch", None)
    elif pending and pending.status != "CANCELLED":
        policy.options = options
        return
    else:
        options.pop("history_pending_batch", None)
    if not preferences.get("automatic_history", True):
        policy.options = options
        return
    existing = s.scalar(select(Batch).where(
        Batch.kind == "sec_history", Batch.policy_key == "sec", Batch.trigger == "automatic",
        Batch.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT", "PAUSED", "PAUSE_REQUESTED", "PARTIAL", "FAILED")),
    ).order_by(Batch.created_at.desc()).limit(1))
    if existing:
        policy.options = {**options, "history_pending_batch": existing.id}
        return
    first, _ = scope_range({"kind": "sec_history", "history_months": preferences["history_months"]})
    end = _sec_recent_days(current.date() - timedelta(days=1), 1)[0]
    previous_first, previous_end = options.get("history_start"), options.get("history_end")
    start, target_end, intent = first, end, "fetch"
    weekly_key = current.strftime("%G-W%V")
    due = not previous_first
    if previous_end and end > date.fromisoformat(previous_end):
        # Recent missing days precede any earlier extension requested in preferences.
        start = max(first, date.fromisoformat(previous_end) + timedelta(days=1))
        due = True
    elif previous_first and first < date.fromisoformat(previous_first):
        target_end = min(end, date.fromisoformat(previous_first) - timedelta(days=1))
        due = True
    elif options.get("sec_weekly") != weekly_key:
        due, intent = True, "refresh"
    if due and start <= target_end:
        batch, _ = _create(s, {
            "kind": "sec_history", "request_id": request_id + ":history",
            "purpose": msg("sec.purpose.history" if intent == "fetch" else "sec.purpose.weekly"),
            "start_date": str(start), "end_date": str(target_end),
            "history_months": preferences["history_months"], "intent": intent,
        }, trigger="automatic", policy_key="sec")
        options["history_pending_batch"] = batch.id
        if not previous_first:
            options["sec_weekly"] = weekly_key
    policy.options = {**policy.options, **options}


def schedule_tick():
    from iirp.jobs.auto_update import ensure_fresh_in_session
    from iirp.jobs.batches import defaults

    with session() as s, s.begin():
        defaults(s)
        market_policy = s.get(CollectionStrategy, "market")
        if market_policy and market_policy.enabled:
            schedule_indices(s)
        ensure_fresh_in_session(s, {"sources": ["market"], "reason": "scheduler"})
        current = now().astimezone(ET)
        strategies = s.scalars(
            select(CollectionStrategy)
            .where(CollectionStrategy.enabled.is_(True), CollectionStrategy.next_run_at <= current)
            .with_for_update(skip_locked=True)
        ).all()
        for policy in strategies:
            request_id = f"auto:{policy.key}:{uuid.uuid4()}"
            if policy.key == "sec" and not sec_configured():
                # Stay enabled but idle: no SEC history round without a real contact.
                policy.next_run_at = current + timedelta(minutes=5)
                continue
            if policy.key == "sec":
                _sec_schedule(s, policy, current, request_id)
            elif policy.key == "market":
                # Prices are a 24-hour cache fetched on demand; home quotes
                # refresh while shown. Nothing is scheduled for this key.
                policy.next_run_at = None
            elif policy.key in ("backup", "maintenance"):
                if not _has_running(s, policy.key):
                    _maintenance_batch(s, policy.key, request_id)
                due = current.replace(
                    hour=3 if policy.key == "backup" else 4, minute=30, second=0, microsecond=0
                )
                policy.next_run_at = due if due > current else due + timedelta(days=1)
            policy.last_run_at = current


def schedule_indices(s):
    """One durable weekly request per index, serialized across coordinators."""
    from sqlalchemy import text

    from iirp.market.constituents import due_indices
    from iirp.market.yahoo import digest
    from iirp.models import ACTIVE

    key = int(digest(["index_constituents"])[0:15], 16)
    if not s.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}):
        return
    for index in due_indices(s):
        if s.scalar(select(Job.id).where(Job.kind == "index_constituents", Job.status.in_(ACTIVE),
                                         Job.target["index_name"].astext == index).limit(1)):
            continue
        target = {"index_name": index, "week": now().strftime("%G-W%V")}
        job = Job(kind="index_constituents", title=msg("job.title.index_constituents", index=index),
                  target=target, idempotency_key=digest(["index_constituents", target, str(uuid.uuid4())]),
                  trigger="automatic", priority=50, progress_total=1)
        s.add(job)
        s.flush()
        s.add(Subscription(job_id=job.id, source="automatic"))
