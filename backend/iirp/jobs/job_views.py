"""Job, schedule and worker views for the task list (keyset pages, compact rows)."""

from datetime import datetime, timedelta

from sqlalchemy import func, select, tuple_

from iirp.db import session
from iirp.models import (
    Job,
    WorkerHeartbeat,
    now,
)

SCOPE = (
    "仅限开发测试：小样验证调度。正式使用保留完整新增发现、三个月回补与按需八年行情规则，P2 接入。"
)


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


SUMMARY_FIELDS = (
    "id", "kind", "title", "status", "trigger", "progress_done", "progress_total", "attempts",
    "error", "created_at", "updated_at", "started_at", "finished_at", "requested_action",
    "control_version",
)


SUMMARY_ERROR_CHARS = 300


def _job_cursor(created_at, identifier):
    return created_at.isoformat() + "~" + identifier


def list_jobs(limit=20, cursor=""):
    """Newest jobs first, keyset paged; never reads checkpoint/result/target payloads."""
    columns = [getattr(Job, key) for key in SUMMARY_FIELDS]
    notice = Job.checkpoint["control_notice"].astext.label("control_notice")
    query = select(*columns, notice).order_by(Job.created_at.desc(), Job.id.desc()).limit(limit + 1)
    if cursor:
        stamp, _, identifier = cursor.partition("~")
        try:
            created = datetime.fromisoformat(stamp)
        except ValueError:
            raise ValueError("任务分页游标无效。") from None
        if not identifier or created.tzinfo is None:
            raise ValueError("任务分页游标无效。")
        query = query.where(tuple_(Job.created_at, Job.id) < tuple_(created, identifier))
    with session() as s:
        rows = [dict(row) for row in s.execute(query).mappings()]
        items = rows[:limit]
        for item in items:
            if item["error"] and len(item["error"]) > SUMMARY_ERROR_CHARS:
                item["error"] = item["error"][:SUMMARY_ERROR_CHARS] + "…"
        return {
            "items": items,
            "worker": worker_view(s),
            "next_cursor": _job_cursor(items[-1]["created_at"], items[-1]["id"])
            if len(rows) > limit else None,
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
