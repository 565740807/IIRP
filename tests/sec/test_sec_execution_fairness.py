"""SEC execution fairness follows actual batch demand, not Job.trigger defaults."""
from datetime import timedelta
from uuid import uuid4

from iirp.api.schemas import CollectionInput
from iirp.db import session
from iirp.jobs import batches
from iirp.jobs.job_views import job_view
from iirp.jobs.queue import claim, fenced
from iirp.models import BatchJob, Filing, Job, RequestScope, SourceBudget, now
from sqlalchemy import select

from tests.jobs.test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401

KINDS = {"sec_discover", "sec_document"}


def scope(s, trigger="automatic", kind="sec_latest"):
    values = CollectionInput.model_validate({
        "request_id": str(uuid4()), "kind": kind,
    }).model_dump(mode="json")
    batch, _ = batches._create(s, values, trigger=trigger, policy_key="sec")
    return s.scalar(select(RequestScope).where(RequestScope.batch_id == batch.id))


def head(s, target_scope, label="head", *, age=0):
    stamp = now() - timedelta(days=age)
    job = batches.add_job(s, target_scope, "sec_discover", {
        "mode": "latest", "round": label,
        "end_date": str(stamp.astimezone(batches.ET).date()),
    })
    job.created_at = stamp
    s.flush()
    return job.id


def document(s, target_scope, accession="0000000001-26-000001"):
    job = batches.add_job(s, target_scope, "sec_document", {"accession": accession})
    s.flush()
    return job.id


def test_live_discovery_gives_the_second_slot_to_document_publication():
    with session() as s, s.begin():
        automatic = scope(s)
        newest = head(s, automatic)
        head(s, automatic, "old-head", age=2)
        filing = document(s, automatic)
    first = claim(KINDS, prefer_latest=True)
    assert first.id == newest
    # Historical business jobs use this default even for automatic batches.
    assert first.trigger == "manual"
    assert claim(KINDS, prefer_latest=True).id == filing


def test_live_document_gives_the_second_slot_to_the_freshest_head():
    with session() as s, s.begin():
        automatic = scope(s)
        filing = document(s, automatic)
        head(s, automatic, "older-head", age=3)
        latest = head(s, automatic, "fresh-head")
    assert claim({"sec_document"}, prefer_latest=True).id == filing
    assert claim(KINDS, prefer_latest=True).id == latest


def test_completion_alternates_discovery_and_documents_without_live_leases():
    with session() as s, s.begin():
        automatic = scope(s)
        first_head = head(s, automatic, "first-head")
        first_document = document(s, automatic)
    discovery = claim(KINDS, prefer_latest=True)
    assert discovery.id == first_head
    assert fenced(discovery, status="SUCCEEDED")
    with session() as s, s.begin():
        automatic = s.get(RequestScope, automatic.id)
        second_head = head(s, automatic, "second-head")
    publication = claim(KINDS, prefer_latest=True)
    assert publication.id == first_document
    assert fenced(publication, status="SUCCEEDED")
    assert claim(KINDS, prefer_latest=True).id == second_head


def test_manual_head_wins_even_when_automatic_documents_have_the_next_turn():
    with session() as s, s.begin():
        automatic = scope(s)
        head(s, automatic, "automatic-head")
        document(s, automatic)
    running = claim(KINDS, prefer_latest=True)
    assert running.kind == "sec_discover"
    with session() as s, s.begin():
        manual = scope(s, trigger="manual")
        foreground = head(s, manual, "manual-head")
    assert claim(KINDS, prefer_latest=True).id == foreground


def test_shared_manual_document_is_foreground_and_reserves_its_source_lane():
    with session() as s, s.begin():
        automatic = scope(s)
        head(s, automatic)
        first = document(s, automatic)
        document(s, automatic, "0000000001-26-000002")
        manual = scope(s, trigger="manual")
        shared = batches.add_job(s, manual, "sec_document", s.get(Job, first).target)
        assert shared.id == first
    foreground = claim(KINDS, prefer_latest=True)
    assert foreground.id == first
    # Shared manual demand cannot be treated as automatic occupancy and permit
    # another automatic operation to take the source while the user is waiting.
    assert claim(KINDS, prefer_latest=True) is None
    assert fenced(foreground, status="SUCCEEDED")
    assert claim(KINDS, prefer_latest=True) is not None


def test_fairness_does_not_bypass_the_shared_source_cooldown():
    with session() as s, s.begin():
        automatic = scope(s)
        identifiers = [head(s, automatic), document(s, automatic)]
        s.add(SourceBudget(provider="sec", next_allowed_at=now() + timedelta(minutes=1), failures=1))
    assert claim(KINDS, prefer_latest=True) is None
    with session() as s:
        assert all(s.get(Job, identifier).attempts == 0 for identifier in identifiers)


def test_aged_historical_document_gets_a_turn_amid_newer_filing_dates():
    with session() as s, s.begin():
        latest_scope = scope(s)
        history_scope = scope(s, kind="sec_history")
        first_head = head(s, latest_scope, "first-head")
        second_head = head(s, latest_scope, "second-head", age=1)
        latest_document = document(s, latest_scope, "0000000001-26-000011")
        old_document = document(s, history_scope, "0000000001-26-000012")
        s.get(Job, old_document).created_at = now() - timedelta(days=2)
        s.add_all([
            Filing(accession="0000000001-26-000011", form="4", filing_date=now().date()),
            Filing(accession="0000000001-26-000012", form="4", filing_date=(now() - timedelta(days=5)).date()),
        ])
    discovery = claim(KINDS, prefer_latest=True)
    assert discovery.id == first_head
    publication = claim(KINDS, prefer_latest=True)
    assert publication.id == latest_document
    assert fenced(publication, status="SUCCEEDED")
    assert claim(KINDS, prefer_latest=True).id == old_document
    assert claim(KINDS, prefer_latest=True).id == second_head


def test_aged_quarterly_scan_gets_a_turn_amid_latest_heads():
    with session() as s, s.begin():
        latest_scope = scope(s)
        history_scope = scope(s, kind="sec_history")
        first_head = head(s, latest_scope, "first-head")
        second_head = head(s, latest_scope, "second-head", age=1)
        document(s, latest_scope)
        historical = batches.add_job(s, history_scope, "sec_discover", {
            "mode": "quarterly", "start_date": "2024-01-01", "end_date": "2024-03-31",
        })
        historical.created_at = now() - timedelta(days=2)
        s.flush()
        historical_id = historical.id
    assert claim(KINDS, prefer_latest=True).id == first_head
    assert claim(KINDS, prefer_latest=True).kind == "sec_document"
    assert claim(KINDS, prefer_latest=True).id == historical_id
    assert claim(KINDS, prefer_latest=True).id == second_head


def test_recent_historical_document_does_not_preempt_latest_discovery():
    with session() as s, s.begin():
        latest_scope = scope(s)
        history_scope = scope(s, kind="sec_history")
        first_head = head(s, latest_scope, "first-head")
        second_head = head(s, latest_scope, "second-head", age=1)
        document(s, history_scope)
    assert claim(KINDS, prefer_latest=True).id == first_head
    assert claim(KINDS, prefer_latest=True).id == second_head


def test_shared_latest_history_claim_remains_latest_after_history_link_changes():
    with session() as s, s.begin():
        latest = scope(s)
        history = scope(s, kind="sec_history")
        head(s, latest)
        shared = document(s, latest, "0000000001-26-000021")
        assert document(s, history, "0000000001-26-000021") == shared
        old = document(s, history, "0000000001-26-000022")
        s.get(Job, old).created_at = now() - timedelta(days=2)
        s.add_all([
            Filing(accession="0000000001-26-000021", form="4", filing_date=now().date()),
            Filing(accession="0000000001-26-000022", form="4", filing_date=(now() - timedelta(days=5)).date()),
        ])
    assert claim(KINDS, prefer_latest=True).kind == "sec_discover"
    first_document = claim(KINDS, prefer_latest=True)
    assert first_document.id == shared
    assert first_document.checkpoint["_queue_sec_class"] == "latest"
    assert "_queue_sec_class" not in job_view(first_document)["checkpoint"]
    assert "_queue_sec_claimed_at" not in job_view(first_document)["checkpoint"]
    assert fenced(first_document, status="SUCCEEDED")
    with session() as s, s.begin():
        link = s.get(BatchJob, (history.id, shared))
        link.active = False
    assert claim(KINDS, prefer_latest=True).id == old


def test_aged_historical_documents_are_fifo_before_filing_recency():
    with session() as s, s.begin():
        latest = scope(s)
        history = scope(s, kind="sec_history")
        head(s, latest)
        fresh = document(s, latest, "0000000001-26-000031")
        oldest = document(s, history, "0000000001-26-000032")
        younger = document(s, history, "0000000001-26-000033")
        s.get(Job, oldest).created_at = now() - timedelta(days=5)
        s.get(Job, younger).created_at = now() - timedelta(days=2)
        s.add_all([
            Filing(accession="0000000001-26-000031", form="4", filing_date=now().date()),
            Filing(accession="0000000001-26-000032", form="4", filing_date=(now() - timedelta(days=10)).date()),
            Filing(accession="0000000001-26-000033", form="4", filing_date=(now() - timedelta(days=3)).date()),
        ])
    assert claim(KINDS, prefer_latest=True).kind == "sec_discover"
    publication = claim(KINDS, prefer_latest=True)
    assert publication.id == fresh
    assert fenced(publication, status="SUCCEEDED")
    first_history = claim(KINDS, prefer_latest=True)
    assert first_history.id == oldest
    assert fenced(first_history, status="SUCCEEDED")
    assert claim(KINDS, prefer_latest=True).id == younger


def test_retry_refreshes_claim_class_and_preserves_it_through_progress_checkpoint():
    with session() as s, s.begin():
        latest = scope(s)
        history = scope(s, kind="sec_history")
        head(s, latest)
        newest = document(s, latest, "0000000001-26-000041")
        old = document(s, history, "0000000001-26-000042")
        old_second = document(s, history, "0000000001-26-000043")
        s.get(Job, old).created_at = now() - timedelta(days=3)
        s.get(Job, old_second).created_at = now() - timedelta(days=2)
    assert claim(KINDS, prefer_latest=True).kind == "sec_discover"
    first = claim(KINDS, prefer_latest=True)
    assert first.id == newest
    initial_stamp = first.checkpoint["_queue_sec_claimed_at"]
    assert fenced(first, checkpoint={"progress": 1}, status="RETRY_WAIT", retry_seconds=0)
    with session() as s:
        saved = s.get(Job, newest)
        assert saved.checkpoint["_queue_sec_class"] == "latest"
        assert saved.checkpoint["progress"] == 1
    historical = claim(KINDS, prefer_latest=True)
    assert historical.id == old
    assert fenced(historical, status="SUCCEEDED")
    retry = claim(KINDS, prefer_latest=True)
    assert retry.id == newest
    assert retry.checkpoint["_queue_sec_claimed_at"] > initial_stamp
    assert fenced(retry, status="SUCCEEDED")
    assert claim(KINDS, prefer_latest=True).id == old_second


def test_empty_document_lane_does_not_starve_aged_quarterly_scans():
    with session() as s, s.begin():
        latest = scope(s)
        history = scope(s, kind="sec_history")
        first = head(s, latest, "first")
        second = head(s, latest, "second", age=1)
        scan = batches.add_job(s, history, "sec_discover", {
            "mode": "quarterly", "start_date": "2024-04-01", "end_date": "2024-06-30",
        })
        scan.created_at = now() - timedelta(days=2)
        s.flush()
        scan_id = scan.id
    assert claim(KINDS, prefer_latest=True).id == first
    assert claim(KINDS, prefer_latest=True).id == scan_id
    assert claim(KINDS, prefer_latest=True).id == second


def test_empty_latest_document_class_does_not_stop_old_document_drain():
    with session() as s, s.begin():
        latest = scope(s)
        history = scope(s, kind="sec_history")
        first_head = head(s, latest, "first-head")
        second_head = head(s, latest, "second-head", age=1)
        third_head = head(s, latest, "third-head", age=2)
        oldest = document(s, history, "0000000001-26-000051")
        next_old = document(s, history, "0000000001-26-000052")
        s.get(Job, oldest).created_at = now() - timedelta(days=4)
        s.get(Job, next_old).created_at = now() - timedelta(days=3)
        s.add_all([
            Filing(accession="0000000001-26-000051", form="4", filing_date=(now() - timedelta(days=10)).date()),
            Filing(accession="0000000001-26-000052", form="4", filing_date=(now() - timedelta(days=8)).date()),
        ])
    first_document = claim({"sec_document"}, prefer_latest=True)
    assert first_document.id == oldest
    assert claim(KINDS, prefer_latest=True).id == first_head
    assert fenced(first_document, status="SUCCEEDED")
    # Fresh discovery remains queued, but no fresh document exists.
    assert claim(KINDS, prefer_latest=True).id == next_old
    assert claim(KINDS, prefer_latest=True).id in (second_head, third_head)


def test_new_wave_of_historical_documents_does_not_wait_an_hour():
    # The planner enqueues the next bounded wave only after old documents
    # finish. A continuous latest head must not keep each wave idle for an hour.
    with session() as s, s.begin():
        latest = scope(s)
        history = scope(s, kind="sec_history")
        first_head = head(s, latest, "first-head")
        second_head = head(s, latest, "second-head", age=1)
        waiting = document(s, history, "0000000001-26-000061")
        s.get(Job, waiting).created_at = now() - timedelta(minutes=3)
        s.add(Filing(
            accession="0000000001-26-000061", form="4",
            filing_date=(now() - timedelta(days=30)).date(),
        ))
    assert claim(KINDS, prefer_latest=True).id == first_head
    assert claim(KINDS, prefer_latest=True).id == waiting
    assert claim(KINDS, prefer_latest=True).id == second_head
