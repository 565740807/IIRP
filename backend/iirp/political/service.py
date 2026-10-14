"""Fenced fact publication and bounded, restartable government backfill planning."""
import hashlib
import json
from datetime import date, timedelta
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from iirp.analysis.calendar import ET
from iirp.db import session
from iirp.jobs.queue import enqueue
from iirp.messages import msg
from iirp.models import (
    ACTIVE,
    Job,
    PoliticalPage,
    PoliticalPerson,
    PoliticalReport,
    PoliticalTransaction,
    SourceObservation,
    SourcePoll,
    now,
)
from iirp.political.config import policy
from iirp.storage.objects import register_object

SOURCES = ("house", "senate", "oge")


def cadence(source, stamp):
    from iirp.jobs.schedule import sec_workday
    config = policy()["cadence"]
    if source == "oge":
        return config["oge_seconds"]
    return config["congress_business_seconds" if sec_workday(stamp.astimezone(ET).date())
                  else "congress_other_seconds"]


def _enqueue(s, kind, target, priority):
    job, _ = enqueue(s, kind, target, "automatic")
    job.priority = priority
    return job


def request_check(s, source, *, trigger="manual"):
    """Reuse the active check and its backfill cursor under the planner lock."""
    from iirp.messages import UserError
    from iirp.sec import contact

    if not contact.configured():
        raise UserError("political.contact_required")
    s.execute(text("SELECT pg_advisory_xact_lock(12812009)"))
    active = s.scalar(select(Job).where(Job.kind == "political_index", Job.status.in_(ACTIVE),
                                       Job.target["source"].astext == source).limit(1))
    if active:
        job, _ = enqueue(s, "political_index", active.target, trigger)
        return job
    current = now()
    s.execute(insert(SourcePoll).values(source="political_" + source, next_poll_at=current)
              .on_conflict_do_nothing())
    poll = s.get(SourcePoll, "political_" + source, with_for_update=True)
    job, _ = enqueue(s, "political_index", {"source": source, "round": current.isoformat(),
                                          "cursor": poll.catchup}, trigger)
    if trigger == "automatic":
        job.priority = 60
    poll.next_poll_at = current + timedelta(seconds=cadence(source, current))
    return job


def schedule(*, startup=False):
    """Small manifests and one page per task keep OCR off provider lanes."""
    from iirp.sec import contact
    network_enabled = contact.configured()
    with session() as s, s.begin():
        if not s.scalar(text("SELECT pg_try_advisory_xact_lock(12812009)")):
            return startup
        current = now()
        active = s.scalar(select(func.count()).select_from(Job).where(Job.status.in_(ACTIVE)))
        capacity = max(0, min(16, 45 - active))
        startup_waiting = False
        for source in SOURCES:
            s.execute(insert(SourcePoll).values(source="political_"+source, next_poll_at=current).on_conflict_do_nothing())
            poll = s.get(SourcePoll, "political_"+source, with_for_update=True)
            if not network_enabled:
                poll.last_error = msg("political.contact_required")
                poll.last_result = {"availability":"unavailable"}
            due = (poll.next_poll_at <= current or poll.last_polled_at is None or
                   (startup and poll.last_success_at is None))
            if network_enabled and due and capacity:
                active_check = s.scalar(select(Job.id).where(Job.kind == "political_index",
                    Job.status.in_(ACTIVE), Job.target["source"].astext == source).limit(1))
                if active_check is None:
                    request_check(s, source, trigger="automatic")
                    capacity -= 1
            if startup and poll.last_success_at is None:
                active_check = s.scalar(select(Job.id).where(Job.kind == "political_index",
                    Job.status.in_(ACTIVE), Job.target["source"].astext == source).limit(1))
                startup_waiting |= active_check is None
        # Existing work is never resubmitted or marked done on restart.
        def pending(kind, target):
            key = hashlib.sha256(json.dumps([kind,target], sort_keys=True).encode()).hexdigest()
            return s.scalar(select(Job.id).where(Job.idempotency_key == key).limit(1)) is not None
        if capacity <= 0:
            return startup_waiting
        version = policy()["collection"]["parser_version"]
        batch = policy()["collection"]["batch_reports"]
        report_order = func.coalesce(PoliticalReport.report_date, PoliticalReport.filed_date)
        # Reserve the configured page slots before admitting more network or
        # manifest work, so an index/download stream cannot starve publication.
        queued_pages = s.scalar(select(func.count()).select_from(Job).where(
            Job.kind == "political_page", Job.status.in_(ACTIVE)))
        slots = min(capacity, policy()["collection"]["max_pending_pages"] - queued_pages)
        pages = s.execute(select(PoliticalPage, PoliticalReport).join(PoliticalReport)
                          .where(PoliticalPage.status == "pending", PoliticalReport.parser_version == version)
                          .order_by(report_order.desc().nulls_last(), PoliticalPage.page)
                          .limit(32)).all()
        for page, report in pages:
            if slots <= 0:
                break
            target = {"source":report.source, "report_id":report.id, "page":page.page,
                      "source_hash":report.source_hash, "media_type":report.details.get("media_type"),
                      "parser_version":version}
            if not pending("political_page", target):
                _enqueue(s, "political_page", target, 80)
                capacity -= 1
                slots -= 1
        queued_manifests = s.scalar(select(func.count()).select_from(Job).where(
            Job.kind == "political_manifest", Job.status.in_(ACTIVE)))
        slots = min(capacity, batch - queued_manifests)
        archived = s.scalars(select(PoliticalReport).where(PoliticalReport.source_hash.is_not(None),
                            (PoliticalReport.parser_version.is_(None) |
                             (PoliticalReport.parser_version != version)))
                            .order_by(report_order.desc().nulls_last()).limit(32)).all()
        for report in archived:
            if slots <= 0:
                break
            target = {"source":report.source, "report_id":report.id, "source_hash":report.source_hash,
                      "media_type":report.details.get("media_type"), "parser_version":version}
            if not pending("political_manifest", target):
                _enqueue(s, "political_manifest", target, 85)
                capacity -= 1
                slots -= 1
        queued_documents = s.scalar(select(func.count()).select_from(Job).where(
            Job.kind == "political_document", Job.status.in_(ACTIVE)))
        slots = min(capacity, batch - queued_documents)
        reports = s.scalars(select(PoliticalReport).where(PoliticalReport.source_hash.is_(None),
                            PoliticalReport.status.in_(("discovered", "superseded")))
                            .order_by(report_order.desc().nulls_last(), PoliticalReport.id)
                            .limit(32)).all()
        for report in reports:
            if not network_enabled or slots <= 0:
                break
            target = {"source":report.source, "report_id":report.id, "url":report.url}
            if not pending("political_document", target):
                _enqueue(s, "political_document", target, 65)
                capacity -= 1
                slots -= 1
        return startup_waiting


def _discover(s, entries, source):
    for entry in entries:
        filed = entry.get("filed_date")
        if isinstance(filed, str):
            filed = date.fromisoformat(filed)
        start = date.fromisoformat(policy()["collection"]["oge_start" if source == "oge" else "congress_start"])
        if filed and filed < start:
            continue
        identity = "|".join([entry["name"], entry.get("district") or "", entry.get("position") or ""])
        person = s.scalar(select(PoliticalPerson).where(PoliticalPerson.source == source,
                                                       PoliticalPerson.identity == identity))
        if person is None:
            person = PoliticalPerson(source=source, identity=identity, name=entry["name"],
                                     district=entry.get("district"), position=entry.get("position"))
            s.add(person)
            s.flush()
        report = s.scalar(select(PoliticalReport).where(PoliticalReport.source == source,
                                                        PoliticalReport.external_id == entry["external_id"]))
        if report is None:
            report_date = filed
            report_date_source = "received_date" if filed else None
            if report_date is None and entry.get("date_source") == "official_index_label":
                report_date = date.fromisoformat(entry["document_date"])
                report_date_source = "official_index_label"
            report = PoliticalReport(source=source, external_id=entry["external_id"], person_id=person.id,
                                     report_date=report_date, report_date_source=report_date_source,
                                     filed_date=filed, url=entry["url"],
                                     amendment_status=entry.get("amendment_status", "original"),
                                     details={key:value for key,value in entry.items() if key not in
                                              ("external_id","name","district","position","filed_date","url","amendment_status")})
            s.add(report)
            s.flush()
    _link_amendments(s, source)


def _link_amendments(s, source):
    amendments = s.scalars(select(PoliticalReport).where(PoliticalReport.source == source,
                                      PoliticalReport.amendment_status.in_(("unresolved", "linked")))).all()
    for report in amendments:
        external = report.details.get("amendment_of_external_id")
        if not external:
            continue
        original = s.scalar(select(PoliticalReport).where(PoliticalReport.source == source,
                             PoliticalReport.external_id == external, PoliticalReport.person_id == report.person_id))
        if original and original.id != report.id:
            report.amendment_of_id = original.id
            report.amendment_status = "linked"
            # Keep original rows and raw files; only the read projection suppresses them.
            original.status = "superseded"


def persist(s, current, data, sources):
    source = current.target["source"]
    poll = s.get(SourcePoll, "political_"+source, with_for_update=True)
    if poll is None:
        poll = SourcePoll(source="political_"+source)
        s.add(poll)
        s.flush()
    for url, saved in sources.items():
        register_object(s, saved)
        s.add(SourceObservation(job_id=current.id, source_hash=saved["sha256"],
                                provider="political_"+source, params={"url":url}))
    result = {"message":msg("political.page_saved" if current.kind == "political_page" else
                             "political.index_saved" if current.kind == "political_index" else "political.document_saved")}
    if data.get("unavailable"):
        if data.get("entries"):
            _discover(s, data["entries"], source)
        if "cursor" in data:
            poll.catchup = data["cursor"]
        poll.last_polled_at = now()
        poll.failures += 1
        poll.last_error = data["unavailable"]
        poll.last_result = {"availability":"unavailable"}
        poll.next_poll_at = now() + timedelta(seconds=min(policy()["cadence"]["retry_max_seconds"],
                                    policy()["cadence"]["retry_initial_seconds"] * 2**min(poll.failures-1, 7)))
        current.status, current.error = "PARTIAL", data["unavailable"]
        if current.kind == "political_document":
            current.status = "RETRY_WAIT"
            current.available_at = poll.next_poll_at
            current.attempts = max(0, current.attempts - 1)
    elif current.kind == "political_index":
        _discover(s, data["entries"], source)
        poll.last_polled_at = poll.last_success_at = now()
        poll.catchup = data.get("cursor")
        if data.get("complete", True):
            poll.last_complete_at = now()
            poll.next_poll_at = now() + timedelta(seconds=cadence(source, now()))
        else:
            poll.next_poll_at = now() + timedelta(seconds=2)
        poll.failures, poll.last_error = 0, None
        poll.last_result = {"availability":"available", "reports_discovered":len(data["entries"])}
        result["reports"] = len(data["entries"])
    elif current.kind == "political_document":
        report = s.get(PoliticalReport, current.target["report_id"], with_for_update=True)
        saved = sources[current.target["url"]]
        report.source_hash = saved["sha256"]
        report.parser_version = None if data.get("needs_manifest") else policy()["collection"]["parser_version"]
        duplicate = s.scalar(select(PoliticalReport).where(
            PoliticalReport.source == source, PoliticalReport.person_id == report.person_id,
            PoliticalReport.source_hash == report.source_hash, PoliticalReport.id != report.id)
            .order_by(PoliticalReport.discovered_at).limit(1))
        if duplicate is not None:
            report.status = "superseded"
            report.parser_version = policy()["collection"]["parser_version"]
            report.details = {**report.details,"duplicate_of_id":duplicate.id}
            result["duplicate_of_id"] = duplicate.id
            current.status = "SUCCEEDED"
            current.result = result
            current.progress_done = 1
            current.finished_at = now()
            current.lease_token = current.lease_until = None
            return
        report.page_count = data["page_count"]
        report.details = {**report.details, "media_type":data["media_type"], "parse_error":data.get("parse_error")}
        if report.status != "superseded":
            report.status = "parsing" if report.page_count or data.get("needs_manifest") else "review"
        for page in range(1, report.page_count+1):
            s.execute(insert(PoliticalPage).values(report_id=report.id, page=page, status="pending",
                       ocr=False, seconds=0, details={}).on_conflict_do_nothing())
        result["pages"] = report.page_count
    elif current.kind == "political_manifest":
        report = s.get(PoliticalReport, current.target["report_id"], with_for_update=True)
        if current.target["parser_version"] != policy()["collection"]["parser_version"]:
            current.status, current.result = "SUCCEEDED", {"superseded":True}
            current.progress_done = 1
            current.finished_at = now()
            current.lease_token = current.lease_until = None
            return
        if data.get("parse_error"):
            report.parser_version = current.target["parser_version"]
            report.details = {**report.details,"parse_error":data["parse_error"]}
            if report.status != "superseded":
                report.status = "review"
            s.execute(update(PoliticalTransaction).where(PoliticalTransaction.report_id == report.id)
                      .values(confidence="review"))
            current.status, current.error = "PARTIAL", data["parse_error"]
            current.result = result
            current.progress_done = 1
            current.finished_at = now()
            current.lease_token = current.lease_until = None
            return
        report.page_count = data["page_count"]
        s.execute(delete(PoliticalTransaction).where(PoliticalTransaction.report_id == report.id,
                                                     PoliticalTransaction.page > report.page_count))
        s.execute(delete(PoliticalPage).where(PoliticalPage.report_id == report.id,
                                              PoliticalPage.page > report.page_count))
        report.parser_version = current.target["parser_version"]
        report.details = {**report.details, "parse_error":data.get("parse_error")}
        report.pages_done = report.ocr_pages = 0
        report.ocr_seconds = 0
        if report.status != "superseded":
            report.status = "parsing" if report.page_count else "review"
        for number in range(1,report.page_count+1):
            page = s.get(PoliticalPage, (report.id,number))
            if page is None:
                page = PoliticalPage(report_id=report.id,page=number)
                s.add(page)
            page.status, page.ocr, page.seconds = "pending", False, 0
            page.details = {}
        result["pages"] = report.page_count
    elif current.kind == "political_page":
        report = s.get(PoliticalReport, current.target["report_id"], with_for_update=True)
        page = s.get(PoliticalPage, (report.id, current.target["page"]), with_for_update=True)
        parsed = data["page_result"]
        if (current.target["parser_version"] != policy()["collection"]["parser_version"]
                or page is None or page.page > report.page_count):
            current.status = "SUCCEEDED"
            current.result = {"superseded":True}
            current.progress_done = 1
            current.finished_at = now()
            current.lease_token = current.lease_until = None
            return
        # Page replacement is atomic and idempotent, including failed parsing.
        s.execute(delete(PoliticalTransaction).where(PoliticalTransaction.report_id == report.id,
                                                       PoliticalTransaction.page == page.page))
        from iirp.political.names import local_matcher
        matcher = local_matcher(s)
        for row in parsed["rows"]:
            row = dict(row)
            for field in ("transaction_date", "notification_date", "expiration_date"):
                if isinstance(row.get(field), str):
                    row[field] = date.fromisoformat(row[field])
            for field, value in row.items():
                if isinstance(value, str) and "\x00" in value:
                    row[field] = value.replace("\x00", "\ufffd")
                    row["confidence"] = "review"
                    row["warnings"] = [*row.get("warnings", []), "text_encoding"]
            matcher.apply(row)
            identity = str(uuid5(NAMESPACE_URL, f"political:{report.id}:{page.page}:{row['row_number']}"))
            s.add(PoliticalTransaction(id=identity, report_id=report.id, page=page.page, **row))
        page.status = "review" if (parsed.get("error") or parsed.get("ocr") or
                                   any(row.get("confidence") == "review" for row in parsed["rows"])) else "parsed"
        page.raw_text = parsed.get("raw_text")
        if page.raw_text:
            page.raw_text = page.raw_text.replace("\x00", "\ufffd")
        page.ocr, page.seconds = parsed.get("ocr", False), parsed.get("seconds", 0)
        page.details = {"error":parsed.get("error"), "row_count":len(parsed["rows"]),
                        "parser_version":current.target["parser_version"]}
        s.flush()
        pages = s.scalars(select(PoliticalPage).where(PoliticalPage.report_id == report.id)).all()
        report.pages_done = sum(p.status != "pending" for p in pages)
        report.ocr_pages = sum(p.ocr for p in pages)
        report.ocr_seconds = sum(float(p.seconds) for p in pages if p.ocr)
        report.parser_version = current.target["parser_version"]
        if report.status != "superseded":
            report.status = "parsing" if report.pages_done < report.page_count else (
                "review" if any(p.status == "review" for p in pages) else "parsed")
        if parsed.get("filed_date") and report.filed_date is None:
            report.filed_date = date.fromisoformat(parsed["filed_date"])
        if parsed.get("amendment"):
            if report.amendment_status == "original":
                report.amendment_status = "unresolved"
            if parsed.get("amendment_of_external_id"):
                report.details = {**report.details,"amendment_of_external_id":parsed["amendment_of_external_id"]}
            _link_amendments(s, source)
        result.update(rows=len(parsed["rows"]), page=page.page, ocr=page.ocr, seconds=page.seconds)
    if current.status not in ("PARTIAL", "RETRY_WAIT"):
        current.status = "SUCCEEDED"
        current.error = None
    current.result = result
    current.progress_done = 1
    if current.status != "RETRY_WAIT":
        current.finished_at = now()
    current.lease_token = current.lease_until = None
