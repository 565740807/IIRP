"""Fresh Linux evidence for newest-first collection and durable update intent."""
from datetime import timedelta
from uuid import uuid4

from iirp import lifecycle
from iirp.business_models import Batch, CollectionStrategy, RequestScope
from iirp.contracts import CollectionInput
from iirp.db import session
from iirp.models import Job, SourceBudget, now
from iirp.queue import claim
from iirp.sec_facts import _quarter_ranges, plan_sec_scope
from sqlalchemy import select
from test_lifecycle import clean_lifecycle, lifecycle_client, lifecycle_database  # noqa: F401


def request(kind, **changes):
    return CollectionInput.model_validate(
        {"request_id": str(uuid4()), "kind": kind, **changes}
    ).model_dump(mode="json")


def test_latest_target_is_today_and_recent_filing_overlap_not_the_history_default():
    start, end = lifecycle.scope_range(request("sec_latest", history_months=12))
    assert end == now().astimezone(lifecycle.ET).date()
    assert 1 <= (end - start).days <= 7


def test_quarter_scans_do_not_expand_before_selected_boundary():
    from datetime import date

    ranges = _quarter_ranges(date(2026, 6, 8), date(2026, 9, 8))
    assert min(start for start, _ in ranges) == date(2026, 6, 8)


def test_saved_defaults_reach_collection_and_remain_frozen():
    lifecycle.update_preferences({"historical_years": 8, "history_months": 6, "comparison": "same_progress"})
    values = request("sec_history")
    result = lifecycle.create_collection(values)
    assert result["batch"]["params"]["history_months"] == 6
    lifecycle.update_preferences({"historical_years": 8, "history_months": 3, "comparison": "same_progress"})
    replay = lifecycle.create_collection(values)
    assert replay["batch_id"] == result["batch_id"]
    assert replay["batch"]["params"]["history_months"] == 6


def test_latest_scope_has_no_quarterly_or_daily_history_work():
    result = lifecycle.create_collection(request("sec_latest"))
    with session() as s, s.begin():
        batch = s.get(Batch, result["batch_id"])
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch.id))
        plan_sec_scope(s, scope, batch, [32], latest_capacity=[8])
        jobs = lifecycle.linked_jobs(s, scope.id)
        assert jobs and all(j.kind == "sec_discover" and j.target["mode"] == "latest" for j in jobs)
        for job in jobs:
            job.status = "SUCCEEDED"
            job.checkpoint = {"sec_scan": {"complete": True}, "discovered_accessions": []}
        s.flush()
        plan_sec_scope(s, scope, batch, [32], latest_capacity=[8])
        assert scope.status == "READY"


def test_latest_document_is_promoted_ahead_of_old_history():
    old = lifecycle.create_collection(request("sec_history"))
    latest = lifecycle.create_collection(request("sec_latest"))
    with session() as s, s.begin():
        old_scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == old["batch_id"]))
        latest_scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == latest["batch_id"]))
        historical = lifecycle.add_job(s, old_scope, "sec_document", {"accession": "history"}, 1)
        historical.created_at = now() - timedelta(days=5)
        shared = lifecycle.add_job(s, old_scope, "sec_document", {"accession": "latest"}, 20)
        promoted = lifecycle.add_job(s, latest_scope, "sec_document", shared.target, 0)
        assert promoted.id == shared.id
        assert promoted.priority == 0
        promoted_id = promoted.id
    assert claim({"sec_document"}, prefer_latest=True).id == promoted_id


def test_shared_sec_cooldown_does_not_consume_filing_attempts():
    result = lifecycle.create_collection(request("sec_latest"))
    with session() as s, s.begin():
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == result["batch_id"]))
        job = lifecycle.add_job(s, scope, "sec_document", {"accession": "waiting"}, 0)
        identifier = job.id
        s.add(SourceBudget(provider="sec", next_allowed_at=now() + timedelta(minutes=15), failures=1))
    assert claim({"sec_document"}) is None
    with session() as s:
        assert s.get(Job, identifier).attempts == 0


def test_frontend_freshness_is_coalesced_and_respects_explicit_pause():
    from iirp.freshness import ensure_fresh

    first = ensure_fresh({"reason": "open", "sources": ["sec", "market"]})
    again = ensure_fresh({"reason": "open", "sources": ["sec", "market"]})
    assert first["batch_ids"] and first["batch_ids"] == again["batch_ids"]
    lifecycle.update_strategy("sec", False)
    third = ensure_fresh({"reason": "open", "sources": ["sec"]})
    assert not third["batch_ids"]
    assert third["sources"]["sec"]["status"] == "paused"
    with session() as s:
        assert not s.get(CollectionStrategy, "sec").enabled


def test_latest_pagination_finishes_only_after_the_final_page():
    result = lifecycle.create_collection(request("sec_latest"))
    with session() as s, s.begin():
        batch = s.get(Batch, result["batch_id"])
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch.id))
        plan_sec_scope(s, scope, batch, [32], latest_capacity=[8])
        first = lifecycle.linked_jobs(s, scope.id)[0]
        first.status = "SUCCEEDED"
        first.checkpoint = {"sec_scan": {"complete": False}}
        tail = lifecycle.add_job(s, scope, "sec_discover", {**first.target, "cursor": "next"})
        s.flush()
        plan_sec_scope(s, scope, batch, [32], latest_capacity=[8])
        assert scope.status != "READY"
        tail.status = "SUCCEEDED"
        tail.checkpoint = {"sec_scan": {"complete": True}}
        s.flush()
        plan_sec_scope(s, scope, batch, [32], latest_capacity=[8])
        assert scope.status == "READY"


def test_history_frontier_waits_for_commit_and_honors_pause():
    from iirp.maintenance import _sec_schedule

    with session() as s, s.begin():
        lifecycle.defaults(s)
        policy = s.get(CollectionStrategy, "sec")
        current = now().astimezone(lifecycle.ET)
        _sec_schedule(s, policy, current, "round-one")
        pending = s.get(Batch, policy.options["history_pending_batch"])
        assert "history_end" not in policy.options
        pending.status = "PAUSED"
        identifier = pending.id
        _sec_schedule(s, policy, current, "round-two")
        assert policy.options["history_pending_batch"] == identifier
        assert "history_end" not in policy.options
        pending.status = "SUCCEEDED"
        _sec_schedule(s, policy, current, "round-three")
        assert policy.options["history_end"] == pending.params["end_date"]


def test_history_off_cancels_automatic_scope_but_preserves_latest_and_manual():
    with session() as s, s.begin():
        auto, _ = lifecycle._create(s, request("sec_history"), trigger="automatic", policy_key="sec")
        auto_id = auto.id
    manual = lifecycle.create_collection(request("sec_history"))
    latest = lifecycle.create_collection(request("sec_latest"))
    lifecycle.update_preferences({"automatic_history": False})
    with session() as s:
        assert s.get(Batch, auto_id).status == "CANCELLED"
        assert s.get(Batch, manual["batch_id"]).status == "QUEUED"
        assert s.get(Batch, latest["batch_id"]).status == "QUEUED"


def test_sec_scope_never_turns_into_market_collection_from_a_stale_ticker():
    result = lifecycle.create_collection(request("sec_history", tickers=["AAPL"]))
    with session() as s:
        scopes = list(s.scalars(select(RequestScope).where(RequestScope.batch_id == result["batch_id"])))
        assert len(scopes) == 1 and scopes[0].symbol == "SEC" and scopes[0].security_id is None


def test_freshness_http_open_and_read_are_distinct(lifecycle_client):  # noqa: F811
    before = lifecycle_client.get("/api/v1/freshness")
    assert before.status_code == 200 and not before.json()["batch_ids"]
    refreshed = lifecycle_client.post("/api/v1/freshness/ensure", json={"reason": "open"}, headers={"X-IIRP-Client": "web"})
    assert refreshed.status_code == 202
    assert len(refreshed.json()["batch_ids"]) == 2
    feed = lifecycle_client.get("/api/v1/feed")
    assert feed.status_code == 200
    assert "pending_filings" in feed.json()


def test_old_latest_pagination_does_not_block_a_new_head_check():
    from iirp.freshness import ensure_fresh
    first = ensure_fresh({"reason": "open", "sources": ["sec"]})
    identifier = first["batch_ids"][0]
    with session() as s, s.begin():
        batch = s.get(Batch, identifier)
        batch.created_at = now() - timedelta(minutes=2)
        policy = s.get(CollectionStrategy, "sec")
        policy.options = {**policy.options, "latest_requested_at": batch.created_at.isoformat()}
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch.id))
        head = lifecycle.add_job(s, scope, "sec_discover", {"mode": "latest", "round": "completed-head"})
        head.status = "SUCCEEDED"
        tail = lifecycle.add_job(s, scope, "sec_discover", {"mode": "latest", "cursor": "older-page"})
        tail.created_at = batch.created_at
    second = ensure_fresh({"reason": "resume", "sources": ["sec"]})
    assert second["batch_ids"][0] == identifier
    with session() as s:
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == identifier))
        jobs = lifecycle.linked_jobs(s, scope.id)
        assert any(job.target.get("cursor") == "older-page" for job in jobs)
        heads = [job for job in jobs if job.kind == "sec_discover" and not job.target.get("cursor") and job.status == "QUEUED"]
        assert len(heads) == 1
        head_id = heads[0].id
    assert claim({"sec_discover"}).id == head_id


def test_shared_document_links_have_a_global_per_tick_budget_without_losing_remainder():
    from iirp.business_models import Filing
    old = lifecycle.create_collection(request("sec_history"))
    latest = [lifecycle.create_collection(request("sec_latest")) for _ in range(2)]
    with session() as s, s.begin():
        old_scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == old["batch_id"]))
        for i in range(12):
            accession = f"0000000001-26-{i:06d}"
            s.add(Filing(accession=accession, form="4", filing_date=now().astimezone(lifecycle.ET).date()))
            lifecycle.add_job(s, old_scope, "sec_document", {"accession": accession})
        s.flush()
        link_budget = [5]
        counts = []
        for item in latest:
            batch = s.get(Batch, item["batch_id"])
            scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch.id))
            plan_sec_scope(s, scope, batch, [32], latest_capacity=[8], document_link_capacity=link_budget)
            counts.append(sum(j.kind == "sec_document" for j in lifecycle.linked_jobs(s, scope.id)))
            assert scope.status != "READY"
        assert counts == [5, 0]
        # Next bounded turn adds every deferred subscription and preserves the demand.
        for item in latest:
            batch = s.get(Batch, item["batch_id"])
            scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch.id))
            plan_sec_scope(s, scope, batch, [32], latest_capacity=[8], document_link_capacity=[100])
            assert sum(j.kind == "sec_document" for j in lifecycle.linked_jobs(s, scope.id)) == 12


def test_promoted_document_backlog_cannot_block_newest_head_planning():
    latest = lifecycle.create_collection(request("sec_latest"))
    with session() as s, s.begin():
        batch = s.get(Batch, latest["batch_id"])
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch.id))
        for i in range(40):
            lifecycle.add_job(s, scope, "sec_document", {"accession": f"old-{i}"})
    # Exercise the actual planner with its latest capacity exhausted, not a manually queued head.
    lifecycle.plan_tick()
    with session() as s:
        head = s.scalar(select(Job).where(Job.kind == "sec_discover", Job.target["mode"].astext == "latest"))
        assert head is not None
        identifier = head.id
    assert claim({"sec_document", "sec_discover"}, prefer_latest=True).id == identifier


def test_unplanned_latest_request_is_reused_after_throttle_period():
    from iirp.freshness import ensure_fresh
    first = ensure_fresh({"reason": "open", "sources": ["sec"]})
    with session() as s, s.begin():
        batch = s.get(Batch, first["batch_ids"][0])
        batch.created_at = now() - timedelta(minutes=3)
        policy = s.get(CollectionStrategy, "sec")
        policy.options = {**policy.options, "latest_requested_at": batch.created_at.isoformat()}
    assert ensure_fresh({"reason": "resume", "sources": ["sec"]})["batch_ids"] == first["batch_ids"]


def test_newly_published_filing_overtakes_older_documents_in_the_latest_lane():
    from iirp.business_models import Filing
    result = lifecycle.create_collection(request("sec_latest"))
    with session() as s, s.begin():
        scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == result["batch_id"]))
        for accession, age in [("0000000001-26-000001", 60), ("0000000001-26-000002", 0)]:
            s.add(Filing(accession=accession, form="4", accepted_at=now()-timedelta(minutes=age), filing_date=now().date()))
            job = lifecycle.add_job(s, scope, "sec_document", {"accession": accession})
            job.created_at = now()-timedelta(minutes=age)
            if age == 0:
                newest = job.id
    assert claim({"sec_document"}, prefer_latest=True).id == newest
