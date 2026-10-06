"""Lightweight change checks and bounded immutable deltas for continuous reading."""

from hashlib import sha256
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select, true

from iirp.business_models import Filing, Issuer
from iirp.db import session
from iirp.feed_index import delta, pending
from iirp.models import Job, now
from iirp.sec_facts import (
    _canonical_kind,
    _offset,
    _session,
    feed_groups,
    feed_watermark,
    open_feed_session,
)

router = APIRouter()


class FeedUpdatesOutput(BaseModel):
    session_id: str
    target_session_id: str
    version: str
    new_count: int
    changed_ids: list[str] = Field(default_factory=list)
    removed_ids: list[str] = Field(default_factory=list)
    groups: list[dict[str, Any]] = Field(default_factory=list)
    next_cursor: str | None = None
    as_of: str
    pending_summary: dict[str, Any] = Field(default_factory=dict)
    pending_filings: list[dict[str, Any]] = Field(default_factory=list)


PENDING_SCOPE = "本地已发现且可见、尚无解析版本的全部申报，含历史回补；不受交易行为筛选影响"
STAGE_LABELS = {
    "discovered": "已发现，等待安排原文获取",
    "waiting_download": "等待 SEC 通道获取原文",
    "fetching_parsing": "正在获取并解析原文",
    "waiting_recovery": "上次原文执行已中断，等待恢复",
    "pause_requested": "等待确认暂停原文任务",
    "cancel_requested": "等待确认取消原文任务",
    "retry_wait": "来源重试等待中",
    "paused": "已暂停原文获取",
    "failed": "原文获取或解析失败，需处理",
    "cancelled": "原文任务已取消，申报仍未解析",
    "needs_review": "原文任务已结束，仍需核对解析结果",
}


def _pending_statement():
    # Start from pending filings, then probe one indexed attempt per accession.
    # Never sort/deduplicate the entire historical SEC job population on a poll.
    accession = Job.target["accession"].astext
    jobs = (
        select(accession.label("accession"), Job.status.label("job_status"), Job.lease_token, Job.lease_until)
        .where(Job.kind == "sec_document", accession == Filing.accession)
        .order_by(Job.created_at.desc(), Job.id.desc())
        .limit(1)
        .correlate(Filing)
        .lateral()
    )
    stage = case(
        (jobs.c.job_status == "QUEUED", "waiting_download"),
        (jobs.c.job_status == "PAUSE_REQUESTED", "pause_requested"),
        (jobs.c.job_status == "CANCEL_REQUESTED", "cancel_requested"),
        ((jobs.c.job_status == "RUNNING") & jobs.c.lease_token.is_not(None)
         & (jobs.c.lease_until > now()), "fetching_parsing"),
        (jobs.c.job_status == "RUNNING", "waiting_recovery"),
        (jobs.c.job_status == "RETRY_WAIT", "retry_wait"),
        (jobs.c.job_status == "PAUSED", "paused"),
        (jobs.c.job_status == "FAILED", "failed"),
        (jobs.c.job_status == "CANCELLED", "cancelled"),
        (jobs.c.job_status.in_(("SUCCEEDED", "PARTIAL")), "needs_review"),
        else_="discovered",
    ).label("stage")
    return (
        select(Filing.accession, Filing.issuer_id, Filing.form, Filing.accepted_at,
               Filing.filing_date, Filing.first_seen_at, stage)
        .outerjoin(jobs, true())
        .where(Filing.visible.is_(True), Filing.current_version.is_(None))
    )


def pending_feed_metadata(s, *, preview=False):
    pending = _pending_statement().subquery()
    counts = dict(s.execute(select(pending.c.stage, func.count()).group_by(pending.c.stage)).all())
    summary = {
        "total": sum(counts.values()),
        "scope": PENDING_SCOPE,
        "stages": [{"id": key, "label": label, "count": counts.get(key, 0)}
                   for key, label in STAGE_LABELS.items() if counts.get(key)],
        "preview_limit": 5,
        "preview_count": 0,
    }
    items = []
    if preview and summary["total"]:
        for row in s.execute(
            select(pending, Issuer.name.label("company"))
            .outerjoin(Issuer, Issuer.id == pending.c.issuer_id)
            .order_by(pending.c.first_seen_at.desc(), pending.c.accession.desc()).limit(5)
        ).mappings():
            items.append({
                "accession": row["accession"], "company": row["company"] or row["issuer_id"],
                "form": row["form"], "accepted_at": row["accepted_at"].isoformat() if row["accepted_at"] else None,
                "filing_date": str(row["filing_date"]) if row["filing_date"] else None,
                "stage": row["stage"], "status": STAGE_LABELS[row["stage"]],
            })
        summary["preview_count"] = len(items)
    return summary, items


def _version(changed, removed):
    identity = [f"{group_key}:{revision_id}" for _, group_key, revision_id in changed]
    return sha256("\n".join(identity + ["-" + key for key in removed]).encode()).hexdigest()


def feed_updates(s, session_id, *, include_groups=False, target_session_id="", cursor=""):
    saved = _session(s, session_id, "feed")
    kind, order = saved.filters["kind"], saved.filters.get("order", "accepted")
    base = feed_watermark(saved)

    def changes(target):
        if target is None:
            return pending(s, base, _canonical_kind(kind), order)
        return delta(s, base, feed_watermark(target), _canonical_kind(kind), order)

    if target_session_id:
        if not include_groups:
            raise ValueError("增量分页需要读取更新内容。")
        target = _session(s, target_session_id, "feed")
        if target.filters.get("delta_from") != saved.id:
            raise ValueError("更新游标与原阅读快照不匹配。")
    else:
        if cursor:
            raise ValueError("增量分页缺少目标快照。")
        target = None
    changed, removed = changes(target)
    offset = _offset(cursor)
    # The first content read opens a target watermark; every delta page is then
    # computed between the same two watermarks and never shifts its boundary.
    if include_groups and target is None and (changed or removed):
        target = open_feed_session(s, kind, order, delta_from=saved.id)
        changed, removed = changes(target)
    summary, preview = pending_feed_metadata(s, preview=include_groups and offset == 0)
    page = changed[offset:offset + 20]
    return {
        "session_id": saved.id,
        "target_session_id": target.id if target else saved.id,
        "version": _version(changed, removed),
        "new_count": len(changed) + len(removed),
        "changed_ids": [group_key for _, group_key, _ in (page if include_groups else changed[:100])],
        "removed_ids": removed if include_groups and offset == 0 else [],
        "groups": feed_groups(s, [revision_id for _, _, revision_id in page], kind, order) if include_groups else [],
        "next_cursor": str(offset + 20) if include_groups and offset + 20 < len(changed) else None,
        "as_of": target.filters["as_of"] if target else saved.filters["as_of"],
        "pending_summary": summary,
        "pending_filings": preview,
    }


@router.get("/api/v1/feed/updates", response_model=FeedUpdatesOutput)
def get_feed_updates(
    session_id: str = Query(min_length=1, max_length=36),
    include_groups: bool = False,
    target_session_id: str = Query(default="", max_length=36),
    cursor: str = Query(default="", max_length=12),
):
    try:
        with session() as s, s.begin():
            return feed_updates(s, session_id, include_groups=include_groups,
                                target_session_id=target_session_id, cursor=cursor)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
