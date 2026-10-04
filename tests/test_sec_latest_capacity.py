"""Latest filing publication progresses despite an unrelated discovery backlog."""
from datetime import timedelta
from uuid import uuid4

from iirp import lifecycle
from iirp.business_models import Batch, BatchJob, Filing, RequestScope
from iirp.contracts import CollectionInput
from iirp.db import session
from iirp.models import Job, now
from iirp.sec_facts import plan_sec_scope
from sqlalchemy import func, insert, select
from test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401

PENDING = ("QUEUED", "RUNNING", "RETRY_WAIT")


def batch_scope(s, kind="sec_latest", trigger="automatic"):
    params = CollectionInput.model_validate({
        "request_id": str(uuid4()), "kind": kind,
    }).model_dump(mode="json")
    batch, _ = lifecycle._create(s, params, trigger=trigger, policy_key="sec")
    scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch.id))
    return batch, scope


def filings(s, count, *, offset=0):
    stamp = now()
    result = []
    for index in range(offset, count + offset):
        filing = Filing(
            accession=f"0000000001-26-{index:06d}", form="4",
            filing_date=stamp.astimezone(lifecycle.ET).date(),
            accepted_at=stamp - timedelta(seconds=index),
            index_url=f"https://www.sec.gov/Archives/edgar/data/1/{index}/ownership.xml",
        )
        s.add(filing)
        result.append(filing)
    s.flush()
    return result


def docs(s):
    return list(s.scalars(select(Job).where(Job.kind == "sec_document")))


def plan(s, batch, scope, *, latest=0):
    plan_sec_scope(s, scope, batch, [0], latest_capacity=[latest])
    s.flush()


def test_thousands_of_discovery_jobs_do_not_starve_newest_documents():
    with session() as s, s.begin():
        old_batch, old_scope = batch_scope(s)
        old_batch.created_at = now() - timedelta(days=2)
        rows = []
        for index in range(1200):
            target = {"mode": "latest", "cursor": str(index), "round": "old"}
            rows.append({
                "id": str(uuid4()), "kind": "sec_discover", "title": "Synthetic older page",
                "target": target, "idempotency_key": lifecycle.digest(["sec_discover", target]),
                "priority": 0,
            })
        s.execute(insert(Job), rows)
        s.execute(insert(BatchJob), [{"scope_id": old_scope.id, "job_id": row["id"]} for row in rows])
        batch, scope = batch_scope(s)
        pending_filings = filings(s, 12)
        plan(s, batch, scope)
        planned = docs(s)
        assert len(planned) == 8
        assert {job.target["accession"] for job in planned} == {f.accession for f in pending_filings[:8]}
        assert scope.checkpoint["unscheduled_documents"] == 4
        # Planning capacity never cancels or falsifies older saved work.
        assert s.scalar(select(func.count()).select_from(Job).where(
            Job.id.in_([row["id"] for row in rows]), Job.status == "QUEUED",
        )) == 1200
        assert old_batch.status == "QUEUED"


def test_shared_documents_count_once_and_free_places_resume_remaining_filings():
    with session() as s, s.begin():
        first, first_scope = batch_scope(s)
        filings(s, 12)
        plan(s, first, first_scope)
        original = {job.id for job in docs(s)}
        assert len(original) == 8
        second, second_scope = batch_scope(s)
        plan(s, second, second_scope)
        assert {job.id for job in docs(s)} == original
        assert sum(job.kind == "sec_document" for job in lifecycle.linked_jobs(s, second_scope.id)) == 8
        assert second_scope.checkpoint["unscheduled_documents"] == 4
        for job in docs(s)[:3]:
            job.status = "SUCCEEDED"
            s.get(Filing, job.target["accession"]).current_version = str(uuid4())
        s.flush()
        plan(s, second, second_scope)
        assert len(docs(s)) == 11
        assert sum(job.status in PENDING for job in docs(s)) == 8
        assert second_scope.checkpoint["unscheduled_documents"] == 1


def test_sharing_historical_work_consumes_new_latest_allowance_without_duplication():
    with session() as s, s.begin():
        history, history_scope = batch_scope(s, "sec_history")
        discovered = filings(s, 12)
        existing = []
        for filing in discovered[:6]:
            existing.append(lifecycle.add_job(s, history_scope, "sec_document", {"accession": filing.accession}).id)
        s.flush()
        latest, latest_scope = batch_scope(s)
        plan(s, latest, latest_scope)
        all_documents = docs(s)
        assert len(all_documents) == 8
        assert set(existing).issubset({job.id for job in all_documents})
        assert all(job.priority == 0 for job in all_documents)
        assert latest_scope.checkpoint["unscheduled_documents"] == 4
        assert history.status == "QUEUED"


def test_manual_latest_uses_its_reserved_capacity_when_auto_documents_are_full():
    with session() as s, s.begin():
        automatic, automatic_scope = batch_scope(s)
        filings(s, 8, offset=20)
        plan(s, automatic, automatic_scope)
        assert len(docs(s)) == 8
        foreground = filings(s, 3)
        manual, manual_scope = batch_scope(s, trigger="manual")
        plan(s, manual, manual_scope, latest=4)
        jobs = lifecycle.linked_jobs(s, manual_scope.id)
        assert {f.accession for f in foreground}.issubset({j.target.get("accession") for j in jobs})
        assert len(docs(s)) == 11


def test_paused_latest_scope_does_not_schedule_or_link_documents():
    with session() as s, s.begin():
        batch, scope = batch_scope(s)
        filings(s, 2)
        batch.status, batch.requested_action = "PAUSED", "pause"
        plan(s, batch, scope)
        assert not lifecycle.linked_jobs(s, scope.id)
        assert not docs(s)
        assert s.get(Batch, batch.id).requested_action == "pause"


def test_new_day_head_is_not_blocked_by_an_unfinished_previous_day_head():
    with session() as s, s.begin():
        old_batch, old_scope = batch_scope(s)
        old_batch.created_at = now() - timedelta(days=1)
        plan(s, old_batch, old_scope)
        old_head = next(job for job in lifecycle.linked_jobs(s, old_scope.id) if job.kind == "sec_discover")
        old_target = dict(old_head.target)
        today, today_scope = batch_scope(s)
        plan(s, today, today_scope)
        heads = [job for job in lifecycle.linked_jobs(s, today_scope.id) if job.kind == "sec_discover"]
        assert len(heads) == 1
        assert heads[0].id != old_head.id
        assert heads[0].target["end_date"] == str(now().astimezone(lifecycle.ET).date())
        assert old_head.status == "QUEUED" and old_head.target == old_target


def test_same_day_batches_share_one_unfinished_head_even_when_capacity_is_available():
    with session() as s, s.begin():
        first, first_scope = batch_scope(s)
        first.created_at = now() - timedelta(minutes=3)
        plan(s, first, first_scope, latest=8)
        saved = dict(first_scope.checkpoint["latest_target"])
        second, second_scope = batch_scope(s)
        plan(s, second, second_scope, latest=8)
        heads = list(s.scalars(select(Job).where(Job.kind == "sec_discover")))
        assert len(heads) == 1
        assert second_scope.checkpoint["latest_target"] == saved
        assert first_scope.checkpoint["latest_target"] == saved
        assert {job.id for job in lifecycle.linked_jobs(s, first_scope.id)} == {job.id for job in lifecycle.linked_jobs(s, second_scope.id)}


def test_a_finished_old_document_gives_its_free_slot_to_the_newest_batch():
    from iirp.business_models import BatchPlanSignal
    from iirp.queue import claim, fenced

    with session() as s, s.begin():
        older, older_scope = batch_scope(s)
        older.created_at = now() - timedelta(days=1)
        older_filings = filings(s, 12, offset=20)
        for filing in older_filings:
            filing.filing_date -= timedelta(days=1)
            filing.accepted_at -= timedelta(days=1)
        s.flush()
        plan(s, older, older_scope)
        assert len(docs(s)) == 8
        today, today_scope = batch_scope(s)
        newest_filings = filings(s, 2)
        newest_accession = newest_filings[0].accession
        today_id, scope_id = today.id, today_scope.id
        plan(s, today, today_scope)
        assert len(docs(s)) == 8
    finished = claim({"sec_document"}, prefer_latest=True)
    assert fenced(finished, status="SUCCEEDED")
    with session() as s:
        assert s.get(BatchPlanSignal, today_id) is not None
    lifecycle.plan_job_scopes(finished.id)
    with session() as s:
        today_documents = [job for job in lifecycle.linked_jobs(s, scope_id) if job.kind == "sec_document"]
        assert [job.target["accession"] for job in today_documents] == [newest_accession]
        assert sum(job.status in PENDING for job in docs(s)) == 8
        assert len(docs(s)) == 9


def test_busy_same_day_head_reservation_preserves_planners_durable_signal():
    from iirp.business_models import BatchPlanSignal
    from sqlalchemy import text

    with session() as s, s.begin():
        batch, target_scope = batch_scope(s)
        identifier = batch.id
        day = str(batch.created_at.astimezone(lifecycle.ET).date())
        token = s.get(BatchPlanSignal, identifier).token
    with session() as owner, owner.begin():
        owner.execute(text("SELECT pg_advisory_xact_lock(:key)"), {
            "key": int(lifecycle.digest(["sec_latest_head", day])[:15], 16),
        })
        assert lifecycle._plan_one(identifier, ("QUEUED", "RUNNING"), [100]) is None
        with session() as s:
            assert s.get(BatchPlanSignal, identifier).token == token
            assert not lifecycle.linked_jobs(s, target_scope.id)
    assert lifecycle._plan_one(identifier, ("QUEUED", "RUNNING"), [100]) is True
    with session() as s:
        assert len(lifecycle.linked_jobs(s, target_scope.id)) == 1


def test_periodic_head_advance_shares_the_planners_same_day_reservation():
    from iirp.freshness import _advance_latest_round
    from sqlalchemy import text

    with session() as s, s.begin():
        batch, target_scope = batch_scope(s)
        identifier, scope_id = batch.id, target_scope.id
        day = str(batch.created_at.astimezone(lifecycle.ET).date())
    with session() as owner, owner.begin():
        owner.execute(text("SELECT pg_advisory_xact_lock(:key)"), {
            "key": int(lifecycle.digest(["sec_latest_head", day])[:15], 16),
        })
        with session() as s, s.begin():
            assert _advance_latest_round(s, s.get(Batch, identifier)) is False
            assert not lifecycle.linked_jobs(s, scope_id)
    with session() as s, s.begin():
        assert _advance_latest_round(s, s.get(Batch, identifier)) is True
    assert lifecycle._plan_one(identifier, ("QUEUED", "RUNNING"), [100]) is True
    with session() as s:
        assert len(lifecycle.linked_jobs(s, scope_id)) == 1
