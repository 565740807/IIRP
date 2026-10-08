"""Is the system working? Current problems grouped by cause, each with a way to act.

Only present conditions count: the worker, SEC configuration and polling,
source cooldowns, disk space, backups, and work that failed in the last day.
Old partial history (for example latest-filing rounds before S1) is not a
problem to act on and is not counted.
"""

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from iirp.api.insider_schemas import Output
from iirp.db import session
from iirp.messages import decode, msg
from iirp.models import CollectionStrategy, Job, SourceBudget, SourcePoll, now

router = APIRouter(prefix="/api/v1/system")

FAILED_WINDOW = timedelta(hours=24)
POLL_STALE = timedelta(minutes=75)
BACKUP_STALE = timedelta(hours=36)
RETRY_LIMIT = 200
# Outcomes that are answers, not faults: a filing ticker Yahoo does not know.
EXPECTED = {"market.identity_fields_missing"}


class HealthAction(Output):
    kind: str
    """``retry`` (POST /system/health/retry with the group key), ``tasks`` (filtered task list),
    ``sec_contact`` (the SEC contact dialog) or ``command``."""
    value: str | None = None


class HealthGroup(Output):
    key: str
    severity: str
    message: str
    count: int = 1
    since: datetime | None = None
    action: HealthAction | None = None


class HealthOutput(Output):
    status: str
    checked_at: datetime
    groups: list[HealthGroup] = Field(default_factory=list)


class RetryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=200)


class RetryOutput(Output):
    retried: int


def _code(error: str | None) -> str:
    message = decode(error)
    if message:
        return message["code"] + ":" + str(sorted((message.get("params") or {}).items()))[:80]
    return (error or "")[:80]


def _failed(s, current):
    """Failed work of the last day by kind and reason."""
    rows = s.execute(
        select(Job.kind, Job.error, func.count(), func.min(Job.updated_at))
        .where(Job.status == "FAILED", Job.updated_at > current - FAILED_WINDOW)
        .group_by(Job.kind, Job.error)
    ).all()
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for kind, error, count, since in rows:
        if (decode(error) or {}).get("code") in EXPECTED:
            continue
        key = (kind, _code(error))
        group = groups.setdefault(key, {"count": 0, "since": since, "error": error})
        group["count"] += count
        group["since"] = min(group["since"], since)
    return [
        {"key": f"failed|{kind}|{code}", "severity": "warning",
         "message": msg("health.failed", kind=kind, count=value["count"], reason=value["error"] or msg("common.unknown")),
         "count": value["count"], "since": value["since"], "action": {"kind": "retry", "value": f"failed|{kind}|{code}"}}
        for (kind, code), value in sorted(groups.items(), key=lambda item: -item[1]["count"])
    ]


def health() -> dict:
    from iirp.jobs.providers import sec_configured
    from iirp.storage.maintenance import storage_state
    from iirp.storage.status import system_database_state

    current = now()
    groups = []
    worker, _ = system_database_state()
    if worker["online"] is False:
        groups.append({"key": "worker_offline", "severity": "critical",
                       "message": msg("health.worker_offline"), "since": worker.get("last_seen"),
                       "action": {"kind": "command", "value": "./iirp start"}})
    elif worker["online"] is None:
        groups.append({"key": "database", "severity": "critical", "message": msg("health.database_unreadable"),
                       "action": {"kind": "command", "value": "./iirp status"}})
        return {"status": "issues", "checked_at": current, "groups": groups}
    with session() as s:
        sec = s.get(CollectionStrategy, "sec")
        if sec is not None and sec.enabled:
            if not sec_configured():
                groups.append({"key": "sec_contact", "severity": "warning", "message": msg("health.sec_contact_missing"),
                               "action": {"kind": "sec_contact"}})
            else:
                poll = s.get(SourcePoll, "sec_latest")
                if poll is not None and poll.failures >= 3:
                    groups.append({"key": "sec_poll_failing", "severity": "warning", "since": poll.last_success_at,
                                   "message": msg("health.sec_poll_failing", count=poll.failures,
                                                  reason=poll.last_error or msg("common.unknown")),
                                   "action": {"kind": "tasks", "value": "sec"}})
                elif poll is not None and poll.last_success_at and current - poll.last_success_at > POLL_STALE:
                    groups.append({"key": "sec_poll_stale", "severity": "warning", "since": poll.last_success_at,
                                   "message": msg("health.sec_poll_stale", time=poll.last_success_at.isoformat()),
                                   "action": {"kind": "tasks", "value": "sec"}})
        for budget in s.scalars(select(SourceBudget)):
            if budget.next_allowed_at > current + timedelta(seconds=60):
                groups.append({"key": f"cooldown|{budget.provider}", "severity": "info",
                               "message": msg("health.source_cooldown", source=budget.provider,
                                              until=budget.next_allowed_at.isoformat()),
                               "action": None})
        groups.extend(_failed(s, current))
        backup = s.get(CollectionStrategy, "backup")
        backup_on = bool(backup and backup.enabled)
    storage = storage_state()
    if storage.get("disk_pressure"):
        groups.append({"key": "disk_low", "severity": "warning",
                       "message": msg("health.disk_low", free=storage.get("free_bytes"), minimum=storage.get("min_free_bytes")),
                       "action": None})
    if backup_on:
        completed = [entry.get("completed_at") for entry in storage.get("backup_list", []) if entry.get("completed_at")]
        latest = max((datetime.fromisoformat(value) for value in completed), default=None)
        if latest is None or current - latest > BACKUP_STALE:
            groups.append({"key": "backup_stale", "severity": "warning", "since": latest,
                           "message": msg("health.backup_stale", time=latest.isoformat()) if latest else msg("health.backup_none"),
                           "action": {"kind": "command", "value": "./iirp backup"}})
    return {"status": "ok" if not groups else "issues", "checked_at": current, "groups": groups}


@router.get("/health", response_model=HealthOutput)
def health_view():
    return health()


@router.post("/health/retry", response_model=RetryOutput)
def retry(body: RetryInput):
    """Retry the failed jobs of one group (at most 200, oldest first)."""
    from iirp.jobs.queue import control

    parts = body.key.split("|", 2)
    if len(parts) != 3 or parts[0] != "failed":
        return {"retried": 0}
    kind, code = parts[1], parts[2]
    current = now()
    with session() as s:
        jobs = [
            job.id for job in s.scalars(
                select(Job).where(Job.status == "FAILED", Job.kind == kind,
                                  Job.updated_at > current - FAILED_WINDOW).order_by(Job.updated_at))
            if _code(job.error) == code
        ][:RETRY_LIMIT]
    for identifier in jobs:
        control(identifier, "retry")
    return {"retried": len(jobs)}
