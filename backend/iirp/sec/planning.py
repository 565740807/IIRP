"""Plan SEC scopes: which quarters, days and documents a demand needs."""

from datetime import date, timedelta

from sqlalchemy import Date, func, or_, select
from sqlalchemy.orm import Session

from iirp.insider.common import ET, _cik, _date, _json
from iirp.models import (
    ACTIVE,
    BatchJob,
    Filing,
    Job,
    SourcePoll,
    TransactionEvent,
    now,
)


def _quarter_ranges(start: date, end: date) -> list[tuple[date, date]]:
    current = date(start.year, ((start.month - 1) // 3) * 3 + 1, 1)
    result = []
    while current <= end:
        following = (
            date(current.year + 1, 1, 1)
            if current.month == 10
            else date(current.year, current.month + 3, 1)
        )
        result.append((max(start, current), min(end, following - timedelta(days=1))))
        current = following
    return list(reversed(result))


def _sec_recent_days(end: date, count=5) -> list[date]:
    # SEC closes on federal holidays. This calendar is independent of NYSE;
    # an exceptional closure still produces an explicit missing-index gap.
    from pandas.tseries.holiday import USFederalHolidayCalendar

    holidays = {
        value.date()
        for value in USFederalHolidayCalendar().holidays(start=end - timedelta(days=30), end=end)
    }
    result, current = [], end
    while len(result) < count:
        if current.weekday() < 5 and current not in holidays:
            result.append(current)
        current -= timedelta(days=1)
    return result


def plan_sec_scope(
    s: Session, scope, batch, capacity: list[int], latest_capacity: list[int] | None = None,
    document_link_capacity: list[int] | None = None,
) -> None:
    """Roll a SEC scope through discovery and each durable document, within budget.

    Discovery manifests are persisted independently of document jobs. A full
    queue merely defers unscheduled filings; failed filings stay visible and are
    never mistaken for parsed ones. Latest checks and recent daily indexes run
    first, followed by quarterly history. All-market discovery is necessary even
    for a single company/person because an index's CIK is not an issuer relation.
    """
    from iirp.jobs.batches import add_job
    from iirp.market.yahoo import digest

    if batch.requested_action or batch.status in {
        "PAUSED",
        "CANCELLED",
        "PAUSE_REQUESTED",
        "CANCEL_REQUESTED",
    }:
        return
    if not isinstance(capacity, list) or len(capacity) != 1 or type(capacity[0]) is not int:
        raise ValueError("SEC 调度容量须为单项整数列表。")
    s.flush()
    jobs = list(
        s.scalars(
            select(Job)
            .join(BatchJob, BatchJob.job_id == Job.id)
            .where(BatchJob.scope_id == scope.id, BatchJob.active.is_(True))
        )
    )
    by_key = {job.idempotency_key: job for job in jobs}
    started, endpoint = scope.start_date, scope.end_date
    if not started or not endpoint:
        raise ValueError("SEC 批次必须具有冻结的起止日期。")
    scan_start = max(started, _date((scope.checkpoint or {}).get("scan_start_date")) or started)
    initial_latest = batch.kind == "sec_latest"
    if initial_latest:
        # Older saved scopes may predate latest-only collection. Keep this chain on its day.
        endpoint = batch.created_at.astimezone(ET).date()
        started = _sec_recent_days(endpoint, 2)[-1]
        scan_start = started
    week = batch.created_at.astimezone(ET).strftime("%G-W%V")
    reconcile_documents = bool(
        batch.params.get("reconcile_documents") or batch.params.get("intent") == "refresh"
    )
    # Discovery and publication need separate bounded queue places. Thousands
    # of saved pagination jobs must not prevent an already discovered filing
    # from receiving its first document job. Count jobs, not subscriptions, so
    # sharing a document across latest rounds does not use capacity twice.
    document_capacity = latest_capacity if latest_capacity is not None else capacity
    document_pending = ("QUEUED", "RUNNING", "RETRY_WAIT")
    if initial_latest and batch.trigger != "manual":
        queued_documents = s.scalar(select(func.count()).select_from(Job).where(
            Job.kind == "sec_document", Job.status.in_(document_pending), Job.priority <= 0,
        ))
        document_capacity = [max(0, 8 - queued_documents)]
    # Manual latest keeps the caller's reserved foreground allowance. This
    # creates no extra execution slots and does not bypass the SEC limiter.

    def ensure(kind, target, priority):
        key = digest([kind, target])
        if key in by_key:
            return by_key[key]
        existing = s.scalar(
            select(Job).where(Job.idempotency_key == key).order_by(Job.created_at.desc()).limit(1)
        )
        if initial_latest and kind == "sec_document":
            budget = document_capacity
        else:
            budget = capacity
        if existing is None and budget[0] <= 0:
            return None
        job = add_job(s, scope, kind, target, priority)
        if existing is None:
            budget[0] -= 1
        by_key[key] = job
        jobs.append(job)
        return job

    # Latest discovery is the source poll (iirp.sec.poll), not a job chain;
    # this demand only schedules documents for the filings it discovered.
    scan_targets = []
    # Quarterly manifests are filtered to the frozen range and newest quarter first.
    # The latest poll handles today's feed; don't enqueue redundant daily
    # scans before publishing the newest historical documents.
    for begin, end in ([] if initial_latest else _quarter_ranges(scan_start, endpoint)):
        scan_targets.append(
            (
                {
                    "mode": "quarterly",
                    "start_date": str(begin),
                    "end_date": str(end),
                    "max_pages": 1,
                    "reconcile_removals": True,
                    "reconcile_documents": reconcile_documents,
                    "round": week,
                },
                20,
                True,
            )
        )
    required_scans, unscheduled_scans = [], 0
    for target, priority, required in scan_targets:
        job = ensure("sec_discover", target, priority)
        if required:
            if job is None:
                unscheduled_scans += 1
            else:
                required_scans.append(job)

    discovered_latest = {
        accession
        for job in jobs
        if job.kind == "sec_discover"
        for accession in job.checkpoint.get("discovered_accessions", [])
    }
    condition = Filing.filing_date.between(scan_start, endpoint)
    if discovered_latest:
        condition = or_(condition, Filing.accession.in_(discovered_latest))
    filings = list(
        s.scalars(
            select(Filing)
            .where(condition, Filing.visible.is_(True))
            .order_by(
                Filing.accepted_at.desc().nulls_last(),
                Filing.filing_date.desc().nulls_last(),
                Filing.accession.desc(),
            )
        )
    )
    missing = [filing for filing in filings if not filing.current_version]
    scheduled_accessions = {
        job.target.get("accession") for job in jobs if job.kind == "sec_document"
    }
    unscheduled_documents = 0
    # A refresh must acquire first documents without dropping source revision
    # checks. Newer parsed filings otherwise consume every bounded queue wave
    # before older DISCOVERED records. Seven first acquisitions followed by one
    # recheck keeps both flows moving; each group retains accepted-time order.
    if reconcile_documents:
        rechecks = iter(filing for filing in filings if filing.current_version)
        document_candidates = []
        for offset in range(0, len(missing), 7):
            document_candidates.extend(missing[offset:offset + 7])
            recheck = next(rechecks, None)
            if recheck is not None:
                document_candidates.append(recheck)
        document_candidates.extend(rechecks)
    else:
        document_candidates = missing
    link_budget = document_link_capacity if document_link_capacity is not None else [100]
    for document_index, filing in enumerate(document_candidates):
        if filing.accession in scheduled_accessions:
            continue
        if link_budget[0] <= 0:
            # Linking shared jobs also writes rows. Bound those writes so a large
            # existing backlog cannot hold the worker's completion locks for a full scan.
            unscheduled_documents += sum(
                value.accession not in scheduled_accessions
                for value in document_candidates[document_index:]
            )
            break
        link_budget[0] -= 1
        # Share another batch's existing filing operation even when it has a
        # different discovery URL alias or this round has no new queue capacity.
        shared_query = select(Job).where(
            Job.kind == "sec_document", Job.target["accession"].astext == filing.accession
        )
        if reconcile_documents:
            shared_query = shared_query.where(Job.target["reconcile_round"].astext == week)
        shared = s.scalar(shared_query.order_by(Job.created_at.desc()).limit(1))
        if shared:
            counted = shared.priority <= 0 and shared.status in document_pending
            add_job(s, scope, shared.kind, shared.target, 0 if initial_latest else 30)
            if (initial_latest and batch.trigger != "manual" and not counted
                    and shared.status in document_pending):
                # Reuse remains possible with a full queue. Promoting existing
                # work consumes the allowance for additional new downloads.
                document_capacity[0] -= 1
            jobs.append(shared)
            scheduled_accessions.add(filing.accession)
            continue
        target = {
            "accession": filing.accession,
            "form": filing.form,
            "index_url": filing.index_url,
            "filing_date": _json(filing.filing_date),
            "accepted_at": _json(filing.accepted_at),
        }
        if reconcile_documents:
            target["reconcile_round"] = week
        if filing.index_url.endswith(("-index.html", "-index.htm")):
            target["submission_url"] = filing.index_url.rsplit("-index.", 1)[0] + ".txt"
        if ensure("sec_document", target, 0 if initial_latest else 30) is None:
            # A full queue must not trigger one database lookup per unscheduled
            # filing. Preserve the remaining count and resume this order later.
            unscheduled_documents += sum(
                value.accession not in scheduled_accessions
                for value in document_candidates[document_index:]
            )
            break
        else:
            scheduled_accessions.add(filing.accession)
    s.flush()
    # An index scope consists of a single quarter per task, so there is no
    # ambiguous “last page succeeded” shortcut across several partial indexes.
    if initial_latest:
        poll = s.get(SourcePoll, "sec_latest")
        scans_done = bool(poll and poll.last_complete_at and poll.last_complete_at >= batch.created_at)
        # Cancelled jobs of the former per-poll discovery chain are not gaps.
        jobs = [job for job in jobs if job.kind != "sec_discover"]
    else:
        scans_done = (
            not unscheduled_scans
            and bool(required_scans)
            and all(job.status == "SUCCEEDED" and job.checkpoint.get("sec_scan", {}).get("complete")
                    for job in required_scans)
        )
    active = [job for job in jobs if job.status in ACTIVE]
    failures = [job for job in jobs if job.status in {"FAILED", "PARTIAL", "CANCELLED"}]
    # Partial Latest/daily discovery can be superseded by a complete quarterly
    # manifest, while genuine document errors always keep the scope incomplete.
    relevant_failures = [
        job
        for job in failures
        if job.kind == "sec_document" or job in required_scans or not scans_done
    ]
    recent_count = batch.params.get("recent_count") or 0
    count = None
    if recent_count:
        statement = (
            select(TransactionEvent)
            .join(Filing, Filing.accession == TransactionEvent.accession)
            .where(TransactionEvent.status == "CURRENT", Filing.visible.is_(True))
        )
        if batch.params.get("issuer_id"):
            statement = statement.where(
                TransactionEvent.issuer_id == _cik(batch.params["issuer_id"])
            )
        if batch.params.get("owner_id"):
            statement = statement.where(
                TransactionEvent.owner_ids.contains([_cik(batch.params["owner_id"])])
            )
        date_basis = batch.params.get("date_basis", "transaction")
        ordering = (
            TransactionEvent.accepted_at
            if date_basis in {"accepted", "accepted_at", "accepted_date"}
            else TransactionEvent.transaction_date
        )
        if date_basis in {"accepted", "accepted_at", "accepted_date"}:
            statement = statement.where(
                func.timezone("America/New_York", TransactionEvent.accepted_at).cast(Date).between(started, endpoint)
            )
        else:
            statement = statement.where(TransactionEvent.transaction_date.between(started, endpoint))
        selected = list(
            s.scalars(
                statement.order_by(ordering.desc().nulls_last(), TransactionEvent.id.desc()).limit(
                    recent_count
                )
            )
        )
        count = len(selected)
        # Stop at the requested boundary even when fewer than N records exist.
        scope.checkpoint = {
            **(scope.checkpoint or {}),
            "history_boundary_reached": scans_done and not missing and count < recent_count,
        }
    successful_rechecks = {
        job.target.get("accession")
        for job in jobs
        if job.kind == "sec_document"
        and job.target.get("reconcile_round") == week
        and job.status == "SUCCEEDED"
    }
    scope.checkpoint = {
        **(scope.checkpoint or {}),
        "scan_start_date": str(scan_start),
        "scan_done": scans_done,
        "discovered_count": len(filings),
        "parsed_count": len(filings) - len(missing),
        "remaining_documents": len(missing),
        "unscheduled_documents": unscheduled_documents,
        "reconcile_documents": reconcile_documents,
        "reconcile_round": week if reconcile_documents else None,
        "remaining_rechecks": sum(
            1
            for filing in filings
            if reconcile_documents
            and filing.current_version
            and filing.accession not in successful_rechecks
        ),
        "requested_count": recent_count or None,
        "observed_count": count,
        "discovery_jobs": len([job for job in jobs if job.kind == "sec_discover"]),
    }
    if active:
        scope.status, scope.wait_reason = (
            "RUNNING",
            next((job.error for job in active if job.error), None),
        )
    elif unscheduled_scans or unscheduled_documents:
        scope.status, scope.wait_reason = (
            "QUEUED",
            "继续处理剩余清单与原文；队列容量仅控制执行节奏。",
        )
    elif initial_latest and batch.trigger == "automatic" and endpoint == now().astimezone(ET).date():
        # Today's demand stays open for the next poll; tomorrow it settles.
        scope.status = "RUNNING"
        scope.wait_reason = next((job.error for job in relevant_failures if job.error), None)
    elif relevant_failures or missing or not scans_done:
        scope.status = "PARTIAL"
        scope.wait_reason = next(
            (job.error for job in relevant_failures if job.error),
            "发现、原文或索引范围仍有缺口，可重试失败项。",
        )
    else:
        scope.status, scope.wait_reason = "READY", None
