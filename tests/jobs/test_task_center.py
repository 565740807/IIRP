"""Task grouping and pagination operate on persisted queue states."""
from datetime import timedelta

import pytest
from iirp.db import session
from iirp.jobs import batch_views, batches
from iirp.models import Batch, BatchJob, Job, RequestScope, now

from tests.jobs.test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401


def test_categories_automatic_history_grouping_and_stable_tie_pagination():
    stamp = now()
    with session() as s, s.begin():
        for i in range(40):
            s.add(Batch(id=f"history-{i:02}", request_id=f"history-{i}", scope_key=f"history-{i}", kind="market_quotes", title="合成历史", params={},
                trigger="automatic" if i < 10 else "manual", policy_key="market" if i < 10 else None, status="SUCCEEDED", created_at=stamp))
        for status in ("RUNNING", "QUEUED", "RETRY_WAIT", "PAUSED", "FAILED", "PARTIAL", "CANCELLED"):
            s.add(Batch(request_id=status, scope_key=status, kind="market_history", title=status, params={}, status=status, created_at=stamp - timedelta(days=1)))
    first = batch_views.list_batches("history")
    assert len(first["items"]) == 15 and first["next_cursor"]
    second = batch_views.list_batches("history", first["next_cursor"])
    third = batch_views.list_batches("history", second["next_cursor"])
    ids = [row["id"] for page in (first, second, third) for row in page["items"]]
    assert len(ids) == len(set(ids)) == 32  # 30 manual + one automatic group + cancelled.
    grouped = [row for page in (first, second, third) for row in page["items"] if row["trigger"] == "automatic"]
    assert len(grouped) == 1 and grouped[0]["history_count"] == 10
    assert len(batch_views.list_batches("history", policy_key="market")["items"]) == 10
    # A persisted planner RUNNING row without an executing job is waiting.
    assert first["counts"] == {"running": 0, "waiting": 3, "attention": 3, "history": 41}
    assert {row["status"] for row in batch_views.list_batches("attention")["items"]} == {"PAUSED", "FAILED", "PARTIAL"}
    with pytest.raises(ValueError, match="分页"):
        batch_views.list_batches("history", "invalid")


def test_activity_uses_live_leases_active_shared_links_and_preserves_control_state():
    stamp = now()
    with session() as s, s.begin():
        jobs = {}
        for name, status, lease in [
            ("shared", "RUNNING", stamp + timedelta(minutes=5)),
            ("expired", "RUNNING", stamp - timedelta(seconds=1)),
            ("unfenced", "RUNNING", stamp + timedelta(minutes=5)),
            ("queued", "QUEUED", None),
            ("retry", "RETRY_WAIT", None),
        ]:
            job = Job(id=name, kind="market_history", title="合成执行状态", target={"symbol": "ORCL"},
                idempotency_key=name, status=status, lease_token="test-fence" if name != "unfenced" and lease else None,
                lease_until=lease, available_at=stamp + timedelta(minutes=1))
            s.add(job)
            jobs[name] = job
        for name, job_name, active in [
            ("live-a", "shared", True), ("live-b", "shared", True),
            ("detached", "shared", False), ("expired", "expired", True),
            ("unfenced", "unfenced", True), ("queued", "queued", True),
            ("retry", "retry", True), ("planning", None, True),
            ("control", None, True),
        ]:
            batch = Batch(id=name, request_id=name, scope_key=name, kind="market_history", title=name,
                params={}, status="PAUSE_REQUESTED" if name == "control" else "RUNNING", control_version=7)
            s.add(batch)
            s.flush()
            scope = RequestScope(id=name, batch_id=name, symbol="ORCL", status="RUNNING")
            s.add(scope)
            s.flush()
            if job_name:
                s.add(BatchJob(scope_id=name, job_id=job_name, active=active))
    running = batch_views.list_batches("running")
    waiting = batch_views.list_batches("waiting")
    assert {row["id"] for row in running["items"]} == {"live-a", "live-b", "control"}
    assert {row["id"] for row in waiting["items"]} == {"detached", "expired", "unfenced", "queued", "retry", "planning"}
    assert running["counts"] == waiting["counts"] == {"running": 3, "waiting": 6, "attention": 0, "history": 0}
    for row in waiting["items"]:
        detail = batch_views.get_batch(row["id"])["batch"]
        assert row["status"] == detail["status"] == "RUNNING"
        assert row["activity_status"] == detail["activity_status"] == "WAITING"
    queued = batch_views.get_batch("queued")["batch"]["items"][0]["progress"]
    retry = batch_views.get_batch("retry")["batch"]["items"][0]["progress"]
    assert queued["stage"] == "等待 Yahoo 通道 · 补齐历史日线"
    assert queued["current_target"] == {"symbol": "ORCL"}
    assert queued["retry_at"] is None
    assert queued["activity_status"] == "QUEUED"
    assert retry["stage"] == "等待来源重试 · 补齐历史日线"
    assert retry["current_target"] == {"symbol": "ORCL"}
    assert retry["retry_at"] == (stamp + timedelta(minutes=1)).isoformat()
    assert retry["activity_status"] == "RETRY_WAIT"
    detached = batch_views.get_batch("detached")["batch"]["items"][0]["progress"]
    assert detached["stage"] == "等待可执行任务" and not detached["current_target"]
    assert detached["activity_status"] == "WAITING"
    with session() as s:
        assert s.get(Batch, "queued").control_version == 7
        assert s.get(Job, "shared").lease_token == "test-fence"
        assert not s.get(BatchJob, ("detached", "shared")).active


def test_scope_targets_live_execution_before_expired_or_detached_shared_jobs():
    stamp = now()
    with session() as s, s.begin():
        s.add(Batch(id="mixed", request_id="mixed", scope_key="mixed", kind="market_history",
            title="合成混合租约", params={}, status="RUNNING"))
        s.flush()
        s.add(RequestScope(id="mixed", batch_id="mixed", symbol="ORCL", status="RUNNING"))
        for name, symbol, live, priority in [("expired", "ORCL", False, 0), ("live", "^GSPC", True, 10), ("detached", "^IXIC", True, 0)]:
            s.add(Job(id=name, kind="market_history", title=name, target={"symbol": symbol}, idempotency_key=name,
                status="RUNNING", priority=priority, lease_token=name,
                lease_until=stamp + timedelta(minutes=5) if live else stamp - timedelta(seconds=1)))
        s.flush()
        for name in ("expired", "live", "detached"):
            s.add(BatchJob(scope_id="mixed", job_id=name, active=name != "detached"))
    detail = batch_views.get_batch("mixed")["batch"]
    progress = detail["items"][0]["progress"]
    assert detail["activity_status"] == progress["activity_status"] == "RUNNING"
    assert progress["current_target"] == {"symbol": "^GSPC"}
    assert progress["stage"] == "补齐历史日线" and not progress["retry_at"]


@pytest.mark.parametrize("category,status", [("waiting", "RUNNING"), ("attention", "PARTIAL")])
def test_repeated_automatic_work_groups_within_category_and_opens_stable_individual_pages(category, status):
    stamp = now()
    with session() as s, s.begin():
        for i in range(34):
            s.add(Batch(id=f"auto-{i:02}", request_id=f"auto-{i}", scope_key=f"auto-{i}", kind="sec_latest",
                title="合成自动计划", params={}, trigger="automatic", policy_key="sec", status=status,
                created_at=stamp))
        s.add(Batch(id="other-category", request_id="other-category", scope_key="other-category", kind="sec_latest",
            title="同计划另一分类", params={}, trigger="automatic", policy_key="sec", status="SUCCEEDED"))
    grouped = batch_views.list_batches(category)
    assert len(grouped["items"]) == 1 and grouped["items"][0]["history_count"] == 34
    assert grouped["counts"][category] == 34
    ids, cursor = [], ""
    while True:
        page = batch_views.list_batches(category, cursor, policy_key="sec")
        ids.extend(row["id"] for row in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert ids == [f"auto-{i:02}" for i in range(33, -1, -1)]
    assert len(set(ids)) == 34 and "other-category" not in ids


def test_personal_drawer_filters_items_and_counts_without_deleting_background():
    from uuid import uuid4

    from iirp.api.schemas import CollectionInput
    from iirp.db import session
    from iirp.jobs import batch_views
    from iirp.models import Batch
    values = CollectionInput.model_validate({"request_id": str(uuid4()), "kind": "sec_latest"}).model_dump(mode="json")
    manual = batches.create_collection(values)
    with session() as s, s.begin():
        auto, _ = batches._create(s, {**values, "request_id": str(uuid4())}, trigger="automatic", policy_key="sec")
        auto_id = auto.id
    personal = batch_views.list_batches("active", view="personal")
    assert [item["id"] for item in personal["items"]] == [manual["batch_id"]]
    assert personal["counts"]["running"] + personal["counts"]["waiting"] == 1
    complete = batch_views.list_batches("active")
    assert complete["counts"]["running"] + complete["counts"]["waiting"] == 2
    with session() as s:
        assert s.get(Batch, auto_id) is not None
