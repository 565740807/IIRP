"""Durable bounded earnings discovery, kept separate from price acquisition.

A search envelope proves only what dates have been examined. Fiscal completeness
is assessed from the actual verified events, never from a candidate page count.
"""

from copy import deepcopy
from datetime import date
from zoneinfo import ZoneInfo

from sqlalchemy import select

from iirp.analytics.calendar import last_completed_session, reaction_session, session_window
from iirp.business_models import EarningsEvent, Security
from iirp.earnings_data import event_dict
from iirp.market_data import digest
from iirp.models import Job, now

ET = ZoneInfo("America/New_York")
ACTIVE = {"QUEUED", "RUNNING", "RETRY_WAIT", "PAUSE_REQUESTED", "CANCEL_REQUESTED"}
STOPPED = {"PAUSED", "CANCELLED", "PAUSE_REQUESTED", "CANCEL_REQUESTED"}
FACT_GAPS = {"fiscal_period_or_precise_time_needs_review", "conflicting_release_observations"}


def _years(params, today, fiscal_end):
    explicit = params.get("current_fiscal_year")
    current = explicit
    if current is None and fiscal_end:
        month_day = tuple(map(int, fiscal_end.split("-")))
        current = today.year + int((today.month, today.day) > month_day)
    if current is None:
        return None, []
    count = int(params.get("historical_years", 8))
    excluded = set(params.get("excluded_years") or [])
    chosen = params.get("years")
    years = range(current - count, current) if chosen is None else chosen
    return current, sorted({int(year) for year in years if year < current and year not in excluded})


def _add_target(state, kind, target):
    key = digest([kind, target])
    if not any(item["key"] == key for item in state["tasks"]):
        state["tasks"].append({"kind": kind, "target": target, "key": key, "job_id": None})


def _freeze(scope, batch, security):
    params = batch.params.get("analysis_params") or batch.params
    created = batch.created_at.astimezone(ET).date()
    fiscal_end = security.metadata_json.get("verified_fiscal_year_end")
    current, years = _years(params, created, fiscal_end)
    # Before fiscal identity is verified, include an extra natural year on both
    # fiscal systems. This is only a search envelope, not claimed price coverage.
    approximate = current or created.year + 1
    earliest = min(years or [approximate - int(params.get("historical_years", 8))])
    start = date(max(1, earliest - 1), 1, 1)
    if scope.start_date:
        start = min(start, scope.start_date)
    envelope = {
        "security_id": security.id,
        "symbol": scope.symbol,
        "start_date": start.isoformat(),
        "end_date": created.isoformat(),
    }
    state = {"version": 1, "as_of_date": str(created), "envelope": envelope, "tasks": []}
    _add_target(state, "earnings_candidates", {**envelope, "offset": 0, "page_size": 100})
    return state


def _event_coverage(s, security, params, state):
    fiscal_end = security.metadata_json.get("verified_fiscal_year_end")
    current, years = _years(params, date.fromisoformat(state["as_of_date"]), fiscal_end)
    reasons = [] if fiscal_end else ["fiscal_year_end_unconfirmed"]
    events = list(
        s.scalars(
            select(EarningsEvent)
            .where(EarningsEvent.security_id == security.id)
            .order_by(EarningsEvent.announced_date, EarningsEvent.id)
        )
    )
    grouped = {}
    for event in events:
        view = event_dict(event)
        if view["is_primary"] and event.fiscal_year and event.fiscal_quarter:
            grouped.setdefault((event.fiscal_year, event.fiscal_quarter), []).append(event)
    calendar = security.calendar or "XNYS"
    matrix = []
    for year in years:
        for quarter in (1, 2, 3, 4):
            items = grouped.get((year, quarter), [])
            reason = (
                "missing_event"
                if not items
                else "duplicate_primary_events"
                if len(items) != 1
                else "estimated_event"
                if items[0].is_estimate
                else "conflicting_release_observations"
                if items[0].status == "CONFLICT"
                else "fiscal_period_unconfirmed"
                if not items[0].verified
                else "precise_announcement_time_unconfirmed"
                if not reaction_session(
                    items[0].announced_at,
                    announced_date=items[0].announced_date,
                    time_precision=items[0].time_precision,
                    calendar=calendar,
                )["opening_attribution"]
                else None
            )
            matrix.append(
                {
                    "fiscal_year": year,
                    "fiscal_quarter": quarter,
                    "event_ids": [event.id for event in items],
                    "status": reason or "VERIFIED",
                }
            )
            if reason:
                reasons.append(reason)
    calendar = security.calendar or "XNYS"
    clock = now()
    completed = last_completed_session(clock, calendar=calendar)
    if params.get("cutoff_date"):
        completed = min(completed, date.fromisoformat(params["cutoff_date"]))
    occurrence_bound = min(clock.astimezone(ET).date(), date.fromisoformat(state["as_of_date"]))
    selected = []
    maturity = []
    for event in events:
        if (
            not event.verified
            or event.is_estimate
            or not event_dict(event)["is_primary"]
            or event.announced_date > occurrence_bound
            or (event.announced_at is not None and event.announced_at > clock)
            or event.fiscal_year not in [*years, current]
        ):
            continue
        view = event_dict(event)
        selected.append(view)
        anchor = reaction_session(
            event.announced_at,
            announced_date=event.announced_date,
            time_precision=event.time_precision,
            calendar=calendar,
        )
        if not anchor["baseline_date"]:
            continue
        days = session_window(date.fromisoformat(anchor["baseline_date"]), 20, 60, calendar)
        maturity.append(
            {
                "event_id": event.id,
                "fiscal_year": event.fiscal_year,
                "fiscal_quarter": event.fiscal_quarter,
                "precise": bool(anchor["opening_attribution"] and event.verified),
                "windows": {
                    str(window): {
                        "end_date": str(days[20 + window]),
                        "mature": days[20 + window] <= completed,
                    }
                    for window in (1, 5, 20, 60)
                },
            }
        )
    unresolved_conflicts = [
        event.id
        for event in events
        if event.status == "CONFLICT"
        and event_dict(event)["is_primary"]
        and event.fiscal_year in [*years, current]
    ]
    if unresolved_conflicts:
        reasons.append("conflicting_release_observations")
    return {
        "unresolved_conflicts": unresolved_conflicts,
        "current_fiscal_year": current,
        "historical_fiscal_years": years,
        "fiscal_year_end": fiscal_end,
        "matrix": matrix,
        "maturity": maturity,
        "reasons": sorted(set(reasons)),
        "selected": selected,
        "cutoff": completed,
    }


def plan_earnings_discovery(s, scope, batch, capacity: list[int]) -> dict:
    """Advance source checkpoints using at most capacity new jobs.

    Caller holds the Batch row lock and runs inside one transaction. This
    function never commits and never calls the price planner. Existing work may
    be subscribed to even at zero new-job capacity; paused batches do nothing.
    Returned fiscal/source details are also stored in scope.checkpoint.
    """
    from iirp.lifecycle import add_job

    if batch.requested_action or batch.status in STOPPED:
        return {
            "fetch_prices": False,
            "price_ready": False,
            "discovery_pending": False,
            "partial": False,
            "reason": "batch_control_intent_preserved",
        }
    security = s.get(Security, scope.security_id)
    if security is None or security.status != "VERIFIED":
        return {
            "fetch_prices": False,
            "price_ready": False,
            "discovery_pending": False,
            "partial": True,
            "reason": "security_identity_or_calendar_unconfirmed",
        }
    state = deepcopy((scope.checkpoint or {}).get("earnings")) or _freeze(scope, batch, security)
    envelope = state["envelope"]
    if not security.issuer_id:
        _add_target(state, "sec_identity", {"symbol": scope.symbol, "security_id": security.id})
    else:
        _add_target(state, "earnings_evidence", {**envelope, "cik": security.issuer_id})
    pending, errors, observed_fact_gaps = False, [], []
    # The task list is append-only and durable. Appended pages/files are visited
    # in this same tick while capacity permits, with idempotent work reuse.
    index = 0
    while index < len(state["tasks"]):
        item = state["tasks"][index]
        index += 1
        job = s.get(Job, item["job_id"]) if item.get("job_id") else None
        if job is None:
            existing = s.scalar(
                select(Job)
                .where(Job.idempotency_key == item["key"])
                .order_by(Job.created_at.desc())
                .limit(1)
            )
            if existing is None and capacity[0] <= 0:
                pending = True
                continue
            job = add_job(s, scope, item["kind"], item["target"], priority=10)
            if existing is None:
                capacity[0] -= 1
            item["job_id"] = job.id
        if job.status in ACTIVE:
            pending = True
            continue
        if job.status not in {"SUCCEEDED", "PARTIAL"}:
            errors.append(job.error or "earnings_source_task_" + job.status.lower())
            continue
        if job.status == "PARTIAL" and job.error and job.error not in FACT_GAPS:
            errors.append(job.error)
        checkpoint = job.checkpoint or {}
        if item["kind"] == "sec_identity":
            if not security.issuer_id:
                errors.append("sec_issuer_identity_unconfirmed")
            continue
        key = (
            "earnings_candidates" if item["kind"] == "earnings_candidates" else "earnings_evidence"
        )
        result = checkpoint.get(key)
        if result is None:
            errors.append("earnings_source_checkpoint_missing")
            continue
        if result.get("reason"):
            errors.append(result["reason"])
        # Old parse-time gaps are evidence, not permanent current-fact errors.
        # Manual review or a later source may already have resolved them.
        observed_fact_gaps.extend(gap for gap in result.get("gaps", []) if gap in FACT_GAPS)
        errors.extend(gap for gap in result.get("gaps", []) if gap not in FACT_GAPS)
        if not result.get("complete") and not result.get("cursor"):
            errors.append(
                "candidate_requested_range_not_closed"
                if item["kind"] == "earnings_candidates"
                else "earnings_evidence_incomplete"
            )
        if item.get("expanded_fingerprint") == digest(result):
            continue
        if item["kind"] == "earnings_candidates":
            if result.get("cursor"):
                _add_target(state, item["kind"], {**envelope, "page_size": 100, **result["cursor"]})
            elif not result.get("complete"):
                errors.append("candidate_requested_range_not_closed")
        else:
            for target in result.get("followups", []):
                target = {**target}
                if security.metadata_json.get("verified_fiscal_year_end"):
                    target["fiscal_year_end"] = security.metadata_json["verified_fiscal_year_end"]
                _add_target(state, item["kind"], target)
            if not result.get("complete"):
                errors.append("earnings_evidence_incomplete")
        item["expanded_fingerprint"] = digest(result)
    # A just-finished identity will be observed on the next durable tick. Its
    # absence must not mark the current batch complete in the intervening tick.
    if security.issuer_id and not any(
        item["kind"] == "earnings_evidence" for item in state["tasks"]
    ):
        pending = True
    details = _event_coverage(
        s, security, batch.params.get("analysis_params") or batch.params, state
    )
    if details["unresolved_conflicts"]:
        errors.append("conflicting_release_observations")
    reasons = sorted(set([*errors, *details["reasons"]]))
    # The price range was fixed when the request was created; finding more
    # quarters never widens it, so prices are fetched once.
    price_ready = bool(details["selected"])
    state["coverage"] = {
        key: value for key, value in details.items() if key not in {"selected", "cutoff"}
    }
    state["coverage"]["price_cutoff"] = str(details["cutoff"])
    state["coverage"]["source_reasons"] = sorted(set(errors))
    state["coverage"]["past_observation_gaps"] = sorted(set(observed_fact_gaps))
    state["coverage"]["source_pending"] = pending
    state["coverage"]["price_ready"] = price_ready
    scope.checkpoint = {**(scope.checkpoint or {}), "earnings": state}
    return {
        "fetch_prices": True,
        "price_ready": price_ready,
        "discovery_pending": pending,
        "partial": bool(reasons),
        "reason": "; ".join(reasons) or None,
        "coverage": state["coverage"],
    }
