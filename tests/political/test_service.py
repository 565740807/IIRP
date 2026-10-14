"""Government backfill publication uses disposable PostgreSQL and synthetic sources."""
import base64
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256

import httpx
import pytest
from iirp.config import settings
from iirp.db import session
from iirp.jobs import handlers
from iirp.jobs.queue import claim, enqueue, fenced
from iirp.messages import msg
from iirp.models import (
    Job,
    PoliticalPage,
    PoliticalPerson,
    PoliticalReport,
    PoliticalTransaction,
    Security,
    SourceObject,
    SourcePoll,
    now,
)
from iirp.political import operations, reads, service
from iirp.political.config import policy
from iirp.storage.objects import save_object
from sqlalchemy import func, select

from tests.jobs.test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_database,
)


@pytest.fixture(autouse=True)
def no_external_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline government service tests must not make source requests")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr("iirp.sec.contact.configured", lambda: True)


class SyntheticRunner:
    """A completed disposable operation; the real supervisor publishes its evidence."""
    def __init__(self, data):
        self.data = data

    def run(self, kind, target, checkpoint):
        assert checkpoint()
        return {"ok": True, "data": deepcopy(self.data)}


def queued(kind, target):
    with session() as s, s.begin():
        job, _ = enqueue(s, kind, target, "automatic")
        identifier = job.id
    lease = claim({kind})
    assert lease is not None and lease.id == identifier
    return lease


def publish(kind, target, data):
    lease = queued(kind, target)
    handlers.execute_business(lease, runner=SyntheticRunner(data))
    with session() as s:
        job = s.get(Job, lease.id)
        assert job.status in ("SUCCEEDED", "PARTIAL"), job.error
    return lease.id


def entry(external="original", **extra):
    return {"external_id": external, "name": "Synthetic Filer", "district": "CA01",
            "filed_date": "2025-03-01", "url": f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2025/{external}.pdf",
            **extra}


def discover(entries, source="house", round="one"):
    return publish("political_index", {"source": source, "round": round}, {"entries": entries})


def report_id(external="original"):
    with session() as s:
        return s.scalar(select(PoliticalReport.id).where(PoliticalReport.external_id == external))


def document(report, pages=2):
    with session() as s:
        row = s.get(PoliticalReport, report)
        target = {"source": row.source, "report_id": row.id, "url": row.url}
    raw = b"%PDF-1.4 synthetic archived source"
    data = {"page_count": pages, "media_type": "application/pdf", "source_documents": [
        {"url": target["url"], "payload": base64.b64encode(raw).decode(),
         "encoding": "base64", "media_type": "application/pdf"}]}
    return publish("political_document", target, data)


def transaction(number=1, **extra):
    return {"row_number": number, "owner": "spouse", "asset_name": "Synthetic Exact Equity",
            "asset_type": "stock", "ticker": None, "ticker_origin": None,
            "transaction_type": "purchase", "transaction_date": "2025-02-20",
            "notification_date": "2025-02-21", "amount_low": "1001", "amount_high": "15000",
            "raw_row": f"Synthetic raw row {number}", "confidence": "parsed", "warnings": [], **extra}


def page_target(report, page):
    with session() as s:
        row = s.get(PoliticalReport, report)
        return {"source": row.source, "report_id": row.id, "page": page,
                "source_hash": row.source_hash, "media_type": row.details["media_type"],
                "parser_version": policy()["collection"]["parser_version"]}


def page_data(rows=None, **extra):
    return {"page_result": {"rows": rows or [transaction()], "raw_text": "Synthetic raw page",
                            "ocr": True, "seconds": 2.5, **extra}}


def postpone_polls():
    with session() as s, s.begin():
        for source in service.SOURCES:
            row = s.get(SourcePoll, "political_" + source)
            if row is None:
                row = SourcePoll(source="political_" + source)
                s.add(row)
            row.next_poll_at = now() + timedelta(days=1)
            row.last_polled_at = row.last_success_at = now()


def test_index_is_idempotent_and_enforces_collection_start_dates():
    rows = [entry(), entry("before", filed_date="2024-12-31"),
            entry("correction", amendment_status="unresolved", amendment_of_external_id="original"),
            entry("unlinked", amendment_status="unresolved")]
    discover(rows)
    discover(rows, round="two")
    discover([entry("oge-before", filed_date="2025-01-19", district=None, position="President"),
              entry("oge-start", filed_date="2025-01-20", district=None, position="President")], source="oge")
    with session() as s:
        assert s.scalar(select(func.count()).select_from(PoliticalPerson)) == 2
        assert s.scalar(select(func.count()).select_from(PoliticalReport)) == 4
        original = s.scalar(select(PoliticalReport).where(PoliticalReport.external_id == "original"))
        amendment = s.scalar(select(PoliticalReport).where(PoliticalReport.external_id == "correction"))
        unresolved = s.scalar(select(PoliticalReport).where(PoliticalReport.external_id == "unlinked"))
        assert original.status == "superseded"
        assert amendment.amendment_status == "linked" and amendment.amendment_of_id == original.id
        assert unresolved.amendment_status == "unresolved" and unresolved.amendment_of_id is None
        assert reads.people(s, "house")["items"][0]["report_count"] == 1
        poll = s.get(SourcePoll, "political_house")
        assert poll.last_result["availability"] == "available" and poll.last_error is None


def test_superseded_originals_are_still_downloaded_and_archived():
    discover([entry(), entry("correction", amendment_status="unresolved",
                             amendment_of_external_id="original")])
    postpone_polls()
    service.schedule()
    with session() as s:
        targets = [job.target["report_id"] for job in s.scalars(select(Job).where(Job.kind == "political_document"))]
        originals = s.scalars(select(PoliticalReport.id)).all()
        assert set(targets) == set(originals)


def test_failed_pdf_inspection_retains_raw_source_through_worker(monkeypatch):
    discover([entry()])
    identifier = report_id()
    raw = b"synthetic broken PDF bytes"
    class OfflineTransport:
        def fetch(self, method, url, data=None):
            return httpx.Response(200, headers={"content-type": "application/pdf"},
                                  content=raw, request=httpx.Request(method, url))
        def close(self):
            pass
    monkeypatch.setattr("iirp.political.transport.Transport", OfflineTransport)
    with session() as s:
        report = s.get(PoliticalReport, identifier)
        target = {"source": "house", "report_id": identifier, "url": report.url}
    response = operations.operation("political_document", target)
    assert response["page_count"] == 0 and response["needs_manifest"]
    publish("political_document", target, response)
    service.schedule()
    lease = claim({"political_manifest"})
    assert lease is not None
    inspected = operations.operation(lease.kind, lease.target)
    assert inspected["page_count"] == 0 and inspected["parse_error"]
    handlers.execute_business(lease, runner=SyntheticRunner(inspected))
    with session() as s:
        report = s.get(PoliticalReport, identifier)
        source = s.get(SourceObject, report.source_hash)
        assert report.status == "review" and report.details["parse_error"]
        assert source.sha256 == sha256(raw).hexdigest() and source.expires_at is None
        assert (settings().runtime_dir / source.relative_path).read_bytes() == raw
        assert s.scalar(select(func.count()).select_from(PoliticalPage)) == 0


def test_page_replacement_is_atomic_and_ocr_progress_counts_each_page_once():
    discover([entry()])
    identifier = report_id()
    document(identifier)
    target = page_target(identifier, 1)
    publish("political_page", target, page_data([transaction(), transaction(2, confidence="review")]))
    # A restarted parser may replace the page, rather than append duplicate rows.
    publish("political_page", target, page_data([transaction()], seconds=3))
    with session() as s:
        assert s.scalar(select(func.count()).select_from(PoliticalTransaction)) == 1
        report = s.get(PoliticalReport, identifier)
        assert (report.pages_done, report.ocr_pages, float(report.ocr_seconds), report.status) == (1, 1, 3, "parsing")
    publish("political_page", page_target(identifier, 2), page_data(error=msg("political.parse_failed", error="SyntheticError")))
    with session() as s:
        report = s.get(PoliticalReport, identifier)
        assert (report.pages_done, report.ocr_pages, float(report.ocr_seconds), report.status) == (2, 2, 5.5, "review")
        assert s.get(PoliticalPage, (identifier, 2)).details["error"]
        assert s.scalar(select(func.count()).select_from(PoliticalTransaction)) == 2


def test_scheduler_resumes_only_unfinished_pages_after_expired_lease():
    discover([entry()])
    identifier = report_id()
    document(identifier)
    postpone_polls()
    service.schedule()
    lease = claim({"political_page"})
    assert lease is not None
    with session() as s, s.begin():
        s.get(Job, lease.id).lease_until = now() - timedelta(seconds=1)
    resumed = claim({"political_page"})
    assert resumed.id == lease.id and resumed.lease_token != lease.lease_token
    handlers.execute_business(resumed, runner=SyntheticRunner(page_data()))
    service.schedule()
    service.schedule()
    with session() as s:
        jobs = s.scalars(select(Job).where(Job.kind == "political_page")).all()
        assert len(jobs) == 2
        assert len([job for job in jobs if job.status == "SUCCEEDED"]) == 1
        assert s.get(PoliticalReport, identifier).pages_done == 1
    next_page = claim({"political_page"})
    handlers.execute_business(next_page, runner=SyntheticRunner(page_data()))
    service.schedule()
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "political_page")) == 2
        assert s.get(PoliticalReport, identifier).status == "review"


def test_expired_lease_cannot_publish_disclosure_facts_or_source_references():
    lease = queued("political_index", {"source": "house", "round": "stale"})
    with session() as s, s.begin():
        s.get(Job, lease.id).lease_until = now() - timedelta(seconds=1)
    fresh = claim({"political_index"})
    assert fresh.id == lease.id and fresh.lease_token != lease.lease_token
    source = save_object(b"Synthetic stale source")
    def writer(s, current):
        service.persist(s, current, {"entries": [entry()]}, {})
    assert not fenced(lease, source=source, business_write=writer)
    with session() as s:
        assert s.scalar(select(func.count()).select_from(PoliticalReport)) == 0
        assert s.scalar(select(func.count()).select_from(SourceObject)) == 0
    assert fenced(fresh, source=source, business_write=writer)
    with session() as s:
        assert s.scalar(select(func.count()).select_from(PoliticalReport)) == 1
        assert s.get(SourceObject, source["sha256"]) is not None


def test_tickers_match_only_unique_exact_local_stock_names():
    discover([entry()])
    identifier = report_id()
    document(identifier, pages=1)
    with session() as s, s.begin():
        s.add_all([Security(symbol="EXACT", name="Synthetic Exact Equity"),
                   Security(symbol="AMB1", name="Ambiguous Equity"),
                   Security(symbol="AMB2", name="Ambiguous Equity"),
                   Security(symbol="BONDLIKE", name="Synthetic Bond")])
    rows = [transaction(), transaction(2, asset_name="Synthetic Exact Equity - Class A"),
            transaction(3, asset_name="Ambiguous Equity"),
            transaction(4, asset_name="Synthetic Bond", asset_type="bond"),
            transaction(5, ticker="REPORTED", ticker_origin="reported")]
    publish("political_page", page_target(identifier, 1), page_data(rows))
    with session() as s:
        trades = s.scalars(select(PoliticalTransaction).order_by(PoliticalTransaction.row_number)).all()
        assert [row.ticker for row in trades] == ["EXACT", "EXACT", None, None, "REPORTED"]
        assert trades[0].ticker_origin == "local_exact" and trades[4].ticker_origin == "reported"


def test_blocked_indexes_record_unavailable_backoff_and_later_recover():
    postpone_polls()
    error = msg("political.source_unavailable", reason=msg("political.reason.blocked"), status=403)
    before = now()
    publish("political_index", {"source": "senate", "round": "blocked-one"}, {"unavailable": error})
    with session() as s:
        poll = s.get(SourcePoll, "political_senate")
        assert poll.failures == 1 and poll.last_result == {"availability": "unavailable"}
        assert poll.last_error == error and poll.last_polled_at >= before
        assert (poll.next_poll_at - poll.last_polled_at).total_seconds() >= policy()["cadence"]["retry_initial_seconds"]
        first_due = poll.next_poll_at
    publish("political_index", {"source": "senate", "round": "blocked-two"}, {"unavailable": error})
    with session() as s:
        assert s.get(SourcePoll, "political_senate").next_poll_at > first_due
    service.schedule()
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "political_index")) == 2
    discover([], source="senate", round="available")
    with session() as s:
        poll = s.get(SourcePoll, "political_senate")
        assert poll.failures == 0 and poll.last_error is None
        assert poll.last_result["availability"] == "available"


def test_cadence_uses_workdays_and_oge_checks_twice_daily():
    from datetime import datetime, timezone
    assert service.cadence("house", datetime(2026, 10, 13, 16, tzinfo=timezone.utc)) == 3600
    assert service.cadence("senate", datetime(2026, 10, 17, 16, tzinfo=timezone.utc)) == 21600
    assert service.cadence("oge", datetime(2026, 10, 17, 16, tzinfo=timezone.utc)) == 43200


def test_unavailable_document_retries_same_job_after_source_cooldown(monkeypatch):
    discover([entry()])
    identifier = report_id()
    postpone_polls()
    with session() as s:
        report = s.get(PoliticalReport, identifier)
        target = {"source": "house", "report_id": identifier, "url": report.url}
    lease = queued("political_document", target)
    error = msg("political.http_unavailable", status=403)
    handlers.execute_business(lease, runner=SyntheticRunner({"unavailable": error}))
    with session() as s:
        job = s.get(Job, lease.id)
        assert job.status == "RETRY_WAIT" and job.attempts == 0
        assert job.error == error and job.finished_at is None and job.lease_token is None
        deadline = job.available_at
        assert deadline == s.get(SourcePoll, "political_house").next_poll_at
        assert s.get(PoliticalReport, identifier).source_hash is None
    assert claim({"political_document"}) is None
    service.schedule()
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "political_document")) == 1
    from iirp.jobs import queue
    clock = deadline + timedelta(seconds=1)
    monkeypatch.setattr(queue, "now", lambda: clock)
    monkeypatch.setattr(service, "now", lambda: clock)
    resumed = claim({"political_document"})
    assert resumed is not None and resumed.id == lease.id and resumed.attempts == 1
    raw = b"%PDF-1.4 synthetic disclosure after cooldown"
    data = {"page_count": 1, "media_type": "application/pdf", "source_documents": [
        {"url": target["url"], "payload": base64.b64encode(raw).decode(),
         "encoding": "base64", "media_type": "application/pdf"}]}
    handlers.execute_business(resumed, runner=SyntheticRunner(data))
    with session() as s:
        assert s.get(Job, lease.id).status == "SUCCEEDED"
        assert s.get(Job, lease.id).error is None
        assert s.get(PoliticalReport, identifier).source_hash == sha256(raw).hexdigest()
        assert s.get(PoliticalPage, (identifier, 1)).status == "pending"
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "political_document")) == 1


def test_manifest_reparses_archived_failed_source_and_ignores_old_parser_results(monkeypatch):
    discover([entry()])
    identifier = report_id()
    with session() as s:
        report = s.get(PoliticalReport, identifier)
        url = report.url
    raw = b"synthetic bytes unsupported by the first PDF inspector"
    data = {"page_count": 0, "media_type": "application/pdf",
            "parse_error": msg("political.parse_failed", error="SyntheticUnsupportedFormat"),
            "source_documents": [{"url": url, "payload": base64.b64encode(raw).decode(),
                                   "encoding": "base64", "media_type": "application/pdf"}]}
    publish("political_document", {"source": "house", "report_id": identifier, "url": url}, data)
    postpone_polls()
    with session() as s:
        report = s.get(PoliticalReport, identifier)
        digest = report.source_hash
    original_version = policy()["collection"]["parser_version"]
    old_target = {"source": "house", "report_id": identifier, "source_hash": digest,
                  "media_type": "application/pdf", "parser_version": original_version}
    old_lease = queued("political_manifest", old_target)
    old_result = operations.operation("political_manifest", old_target)
    assert old_result["page_count"] == 0 and old_result["parse_error"]
    updated_policy = deepcopy(policy())
    updated_policy["collection"]["parser_version"] = "synthetic-new-parser"
    monkeypatch.setattr(service, "policy", lambda: updated_policy)
    monkeypatch.setattr(operations, "policy", lambda: updated_policy)
    # An updated inspector can recognize the same retained bytes without downloading again.
    def updated_inspector(payload):
        assert payload == raw
        return 2
    monkeypatch.setattr("iirp.political.parse.inspect_pdf", updated_inspector)
    service.schedule()
    with session() as s:
        manifest = s.scalar(select(Job).where(Job.kind == "political_manifest", Job.status == "QUEUED"))
        assert manifest.target["parser_version"] == "synthetic-new-parser"
        target = manifest.target
    new_lease = claim({"political_manifest"})
    handlers.execute_business(new_lease, runner=SyntheticRunner(operations.operation("political_manifest", target)))
    # A still-owned older operation is obsolete because its parser contract changed.
    handlers.execute_business(old_lease, runner=SyntheticRunner(old_result))
    stale_page = {**old_target, "page": 1}
    publish("political_page", stale_page, page_data([transaction(asset_name="Must not publish")]))
    with session() as s:
        report = s.get(PoliticalReport, identifier)
        assert report.source_hash == digest and report.parser_version == "synthetic-new-parser"
        assert (report.page_count, report.pages_done, report.ocr_pages, float(report.ocr_seconds)) == (2, 0, 0, 0)
        assert [page.status for page in s.scalars(select(PoliticalPage).order_by(PoliticalPage.page))] == ["pending", "pending"]
        assert s.scalar(select(func.count()).select_from(PoliticalTransaction)) == 0
        assert s.get(Job, old_lease.id).result["superseded"]
        source = s.get(SourceObject, digest)
        assert source.expires_at is None
        assert (settings().runtime_dir / source.relative_path).read_bytes() == raw
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "political_document")) == 1
    # The new manifest resumes page work through the same bounded scheduler.
    service.schedule()
    with session() as s:
        page_jobs = s.scalars(select(Job).where(Job.kind == "political_page", Job.status == "QUEUED")).all()
        assert len(page_jobs) == 2 and all(job.target["parser_version"] == "synthetic-new-parser" for job in page_jobs)
    for _ in range(2):
        lease = claim({"political_page"})
        handlers.execute_business(lease, runner=SyntheticRunner(page_data()))
    with session() as s:
        report = s.get(PoliticalReport, identifier)
        assert report.status == "review" and report.source_hash == digest
        assert (report.pages_done, report.ocr_pages, float(report.ocr_seconds)) == (2, 2, 5)


def test_new_manifest_resets_completed_pages_and_removes_rows_beyond_new_page_count(monkeypatch):
    discover([entry()])
    identifier = report_id()
    document(identifier, pages=3)
    for number in range(1, 4):
        publish("political_page", page_target(identifier, number), page_data())
    with session() as s:
        digest = s.get(PoliticalReport, identifier).source_hash
        assert s.get(PoliticalReport, identifier).pages_done == 3
    updated_policy = deepcopy(policy())
    updated_policy["collection"]["parser_version"] = "synthetic-shorter-manifest"
    monkeypatch.setattr(service, "policy", lambda: updated_policy)
    monkeypatch.setattr(operations, "policy", lambda: updated_policy)
    postpone_polls()
    target = {"source": "house", "report_id": identifier, "source_hash": digest,
              "media_type": "application/pdf", "parser_version": "synthetic-shorter-manifest"}
    monkeypatch.setattr("iirp.political.parse.inspect_pdf", lambda payload: 1)
    publish("political_manifest", target, operations.operation("political_manifest", target))
    with session() as s:
        report = s.get(PoliticalReport, identifier)
        assert report.source_hash == digest and report.pages_done == 0 and report.ocr_pages == 0
        pages = s.scalars(select(PoliticalPage).where(PoliticalPage.report_id == identifier)).all()
        assert [(page.page, page.status) for page in pages] == [(1, "pending")]
        assert s.scalars(select(PoliticalTransaction.page).where(PoliticalTransaction.report_id == identifier)).all() == [1]
    publish("political_page", {**target, "page": 1}, page_data())
    obsolete = publish("political_page", {**target, "page": 2}, page_data())
    with session() as s:
        report = s.get(PoliticalReport, identifier)
        assert (report.status, report.page_count, report.pages_done, report.ocr_pages) == ("review", 1, 1, 1)
        assert s.scalar(select(func.count()).select_from(PoliticalTransaction)) == 1
        assert s.get(Job, obsolete).result["superseded"]


def test_decoded_nul_text_is_flagged_without_changing_archived_bytes():
    discover([entry()])
    identifier = report_id()
    document(identifier, pages=1)
    with session() as s:
        report = s.get(PoliticalReport, identifier)
        source = s.get(SourceObject, report.source_hash)
        before = (settings().runtime_dir / source.relative_path).read_bytes()
        digest = report.source_hash
    publish("political_page", page_target(identifier, 1),
            page_data([transaction(asset_name="Synthetic\x00Equity", raw_row="Raw\x00row")],
                      raw_text="Page\x00text"))
    with session() as s:
        row = s.scalar(select(PoliticalTransaction))
        assert row.confidence == "review" and "text_encoding" in row.warnings
        assert "\x00" not in row.asset_name and "\x00" not in row.raw_row
        assert "\x00" not in s.get(PoliticalPage, (identifier, 1)).raw_text
        assert s.get(PoliticalReport, identifier).source_hash == digest
        assert (settings().runtime_dir / source.relative_path).read_bytes() == before


def test_missing_contact_does_not_plan_network_work_and_allows_local_ocr(monkeypatch):
    discover([entry(), entry("archived")])
    archived = report_id("archived")
    document(archived, pages=1)
    monkeypatch.setattr("iirp.sec.contact.configured", lambda: False)
    service.schedule()
    with session() as s:
        network = s.scalars(select(Job).where(Job.kind.in_(("political_index", "political_document")),
                                               Job.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT")))).all()
        assert not network
        for source in service.SOURCES:
            poll = s.get(SourcePoll, "political_" + source)
            assert poll.last_result == {"availability": "unavailable"}
            assert poll.last_error == msg("political.contact_required")
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "political_manifest")) == 0
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "political_page")) == 1
        source_hash = s.get(PoliticalReport, archived).source_hash
    lease = claim({"political_page"})
    assert lease is not None
    handlers.execute_business(lease, runner=SyntheticRunner(page_data()))
    with session() as s:
        report = s.get(PoliticalReport, archived)
        assert report.status == "review" and report.pages_done == 1 and report.ocr_pages == 1
        assert report.source_hash == source_hash


def test_failed_new_manifest_preserves_previous_pages_and_marks_rows_for_review(monkeypatch):
    discover([entry()])
    identifier = report_id()
    document(identifier, pages=1)
    publish("political_page", page_target(identifier, 1), page_data())
    with session() as s:
        report = s.get(PoliticalReport, identifier)
        digest = report.source_hash
        page_text = s.get(PoliticalPage, (identifier, 1)).raw_text
    updated_policy = deepcopy(policy())
    updated_policy["collection"]["parser_version"] = "synthetic-failed-new-inspector"
    monkeypatch.setattr(service, "policy", lambda: updated_policy)
    monkeypatch.setattr(operations, "policy", lambda: updated_policy)
    target = {"source": "house", "report_id": identifier, "source_hash": digest,
              "media_type": "application/pdf", "parser_version": "synthetic-failed-new-inspector"}
    result = operations.operation("political_manifest", target)
    assert result["page_count"] == 0 and result["parse_error"]
    job_id = publish("political_manifest", target, result)
    with session() as s:
        report = s.get(PoliticalReport, identifier)
        assert report.source_hash == digest and report.status == "review"
        assert (report.page_count, report.pages_done, report.ocr_pages) == (1, 1, 1)
        assert report.details["parse_error"] == result["parse_error"]
        assert s.get(PoliticalPage, (identifier, 1)).raw_text == page_text
        rows = s.scalars(select(PoliticalTransaction)).all()
        assert len(rows) == 1 and rows[0].confidence == "review"
        assert s.get(SourceObject, digest).expires_at is None
        assert s.get(Job, job_id).status == "PARTIAL"


def test_empty_ocr_page_remains_reviewable_even_without_transaction_rows():
    discover([entry()])
    identifier = report_id()
    document(identifier, pages=1)
    data = {"page_result": {"rows": [], "raw_text": "Unreadable synthetic transaction table",
                           "ocr": True, "seconds": 1}}
    publish("political_page", page_target(identifier, 1), data)
    with session() as s:
        report = s.get(PoliticalReport, identifier)
        page = s.get(PoliticalPage, (identifier, 1))
        assert report.status == page.status == "review"
        assert report.pages_done == 1 and report.source_hash is not None
        assert page.raw_text == data["page_result"]["raw_text"]


def test_completed_index_restores_source_cadence_after_a_manual_check():
    from iirp.political.config import policy
    delay = policy()["cadence"]["oge_seconds"]
    with session() as s, s.begin():
        s.add(SourcePoll(source="political_oge", next_poll_at=now()+timedelta(days=2)))
    before = now()
    discover([entry("manual-index")], source="oge")
    after = now()
    with session() as s:
        poll = s.get(SourcePoll, "political_oge")
        assert before+timedelta(seconds=delay) <= poll.next_poll_at <= after+timedelta(seconds=delay)
        assert poll.last_complete_at is not None


def test_new_document_uses_existing_page_inspection_without_another_manifest():
    discover([entry()])
    identifier = report_id()
    document(identifier, pages=2)
    service.schedule()
    with session() as s:
        report = s.get(PoliticalReport, identifier)
        assert report.parser_version == policy()["collection"]["parser_version"]
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "political_manifest")) == 0
        pages = s.scalars(select(Job).where(Job.kind == "political_page")).all()
        assert len(pages) == 2 and all(job.status == "QUEUED" for job in pages)


def test_obsolete_page_job_does_not_read_or_recognize_its_archived_file(monkeypatch):
    from pathlib import Path
    def forbidden(*args, **kwargs):
        raise AssertionError("Obsolete parser work must not occupy the OCR lane")
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    assert operations.operation("political_page", {"parser_version": "obsolete", "source_hash": "unused"}) == {
        "page_result": {"rows": []}}
    assert operations.operation("political_manifest", {"parser_version": "obsolete", "source_hash": "unused"}) == {
        "page_count": 0}


def test_original_is_permanent_before_pdf_inspection(monkeypatch):
    from pathlib import Path
    payload = (Path(__file__).parent / "fixtures/electronic.pdf").read_bytes()
    discover([entry()])
    identifier = report_id()
    with session() as s:
        url = s.get(PoliticalReport, identifier).url
    class Download:
        def fetch(self, method, address):
            assert address == url
            return httpx.Response(200, content=payload, headers={"content-type": "application/pdf"},
                                  request=httpx.Request(method, address))
        def close(self):
            pass
    monkeypatch.setattr("iirp.political.transport.Transport", Download)
    def forbidden(payload):
        raise AssertionError("The network unit cannot inspect an unarchived PDF")
    monkeypatch.setattr("iirp.political.parse.inspect_pdf", forbidden)
    target = {"source": "house", "report_id": identifier, "url": url}
    data = operations.operation("political_document", target)
    assert data["needs_manifest"] and data["page_count"] == 0
    publish("political_document", target, data)
    digest = sha256(payload).hexdigest()
    with session() as s:
        report = s.get(PoliticalReport, identifier)
        assert report.source_hash == digest and report.parser_version is None
        assert report.status == "parsing"
        assert s.get(SourceObject, digest).expires_at is None
    service.schedule()
    def inspect(saved):
        assert saved == payload
        with session() as s:
            assert s.get(SourceObject, digest).expires_at is None
            assert s.get(PoliticalReport, identifier).source_hash == digest
        return 2
    monkeypatch.setattr("iirp.political.parse.inspect_pdf", inspect)
    lease = claim({"political_manifest"})
    assert lease is not None
    handlers.execute_business(lease, runner=SyntheticRunner(operations.operation(lease.kind, lease.target)))
    service.schedule()
    with session() as s:
        assert s.get(PoliticalReport, identifier).page_count == 2
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "political_page")) == 2


def test_never_checked_sources_ignore_initial_interval_and_reuse_active_check():
    postpone_polls()
    with session() as s, s.begin():
        poll = s.get(SourcePoll, "political_oge")
        poll.last_polled_at = poll.last_success_at = None
        poll.catchup = {"offset": 12}
    service.schedule()
    service.schedule()
    with session() as s:
        jobs = s.scalars(select(Job).where(Job.kind == "political_index")).all()
        assert len(jobs) == 1 and jobs[0].target["source"] == "oge"
        assert jobs[0].target["cursor"] == {"offset": 12}


def test_unsuccessful_source_retries_once_at_startup_then_keeps_backoff():
    postpone_polls()
    with session() as s, s.begin():
        poll = s.get(SourcePoll, "political_oge")
        poll.last_success_at = None
        poll.failures = 1
    service.schedule()
    assert claim({"political_index"}) is None
    service.schedule(startup=True)
    lease = claim({"political_index"})
    assert lease and lease.target["source"] == "oge"
    handlers.execute_business(lease, runner=SyntheticRunner({"unavailable": msg("political.contact_required")}))
    service.schedule()
    assert claim({"political_index"}) is None


def test_reparsing_replaces_rows_with_repeatable_identifiers_and_values():
    discover([entry()])
    identifier = report_id()
    document(identifier, pages=1)
    target = page_target(identifier, 1)
    data = page_data([transaction(), transaction(2)])
    publish("political_page", target, data)
    with session() as s:
        from iirp.api.political_schemas import PoliticalTransactionView
        first = [PoliticalTransactionView.model_validate(row).model_dump() for row in reads.report(s, identifier)["rows"]]
    publish("political_page", target, data)
    with session() as s:
        second = [PoliticalTransactionView.model_validate(row).model_dump() for row in reads.report(s, identifier)["rows"]]
        assert first == second
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "political_document")) == 1


def test_startup_check_is_deferred_until_queue_capacity_is_available():
    from sqlalchemy import update

    postpone_polls()
    with session() as s, s.begin():
        s.get(SourcePoll, "political_oge").last_success_at = None
        for number in range(45):
            enqueue(s, "fixture_check", {"number": number}, "automatic")
    assert service.schedule(startup=True) is True
    with session() as s, s.begin():
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "political_index")) == 0
        s.execute(update(Job).where(Job.kind == "fixture_check").values(status="SUCCEEDED"))
    assert service.schedule(startup=True) is False
    lease = claim({"political_index"})
    assert lease and lease.target["source"] == "oge"


def test_current_parser_replaces_receipt_rows_from_retained_source_without_a_download(monkeypatch):
    import json

    current_policy = policy()
    old_policy = deepcopy(current_policy)
    old_policy["collection"]["parser_version"] = "obsolete-parser"
    monkeypatch.setattr(service, "policy", lambda: old_policy)
    monkeypatch.setattr(operations, "policy", lambda: old_policy)
    from iirp.political.parse import _table_rows

    discover([entry()])
    identifier = report_id()
    document(identifier, pages=1)
    publish("political_page", {**page_target(identifier, 1), "parser_version": "obsolete-parser"}, page_data([
        transaction(asset_name="OGE Received", transaction_type="unknown", amount_low=None, amount_high=None),
        transaction(2, asset_name="OGE Energy Corp")]))
    postpone_polls()
    monkeypatch.setattr(service, "policy", lambda: current_policy)
    monkeypatch.setattr(operations, "policy", lambda: current_policy)
    with session() as s:
        report = s.get(PoliticalReport, identifier)
        digest = report.source_hash
    service.schedule()
    with session() as s:
        job = s.scalar(select(Job).where(Job.kind == "political_manifest", Job.status == "QUEUED"))
        assert job.target["source_hash"] == digest
        assert job.target["parser_version"] == policy()["collection"]["parser_version"]
    lease = claim({"political_manifest"})
    handlers.execute_business(lease, runner=SyntheticRunner({"page_count": 1, "media_type": "application/pdf"}))
    service.schedule()
    parsed = _table_rows([
        ["Asset", "Transaction Type", "Date", "Amount"],
        ["OGE Received", "", "03/01/2025", ""],
        ["OGE Energy Corp", "Purchase", "02/20/2025", "$1,001-$15,000"],
    ])
    lease = claim({"political_page"})
    handlers.execute_business(lease, runner=SyntheticRunner(page_data(json.loads(json.dumps(parsed, default=str)))))
    with session() as s:
        assert list(s.scalars(select(PoliticalTransaction.asset_name))) == ["OGE Energy Corp"]
        report = s.get(PoliticalReport, identifier)
        assert report.source_hash == digest and report.pages_done == 1
        assert report.parser_version == policy()["collection"]["parser_version"]
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "political_document")) == 1
        assert s.get(SourceObject, digest).expires_at is None
