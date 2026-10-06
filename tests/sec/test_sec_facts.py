"""Real, isolated PostgreSQL evidence for SEC persistence and reading snapshots.

All Ownership inputs are explicitly synthetic fixtures, not live coverage claims.
"""

import hashlib
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from iirp.config import ROOT, settings
from iirp.db import engine, session
from iirp.insider.facts import (
    entity_history,
    feed,
    feed_group,
    persist_discovery,
    persist_document,
    plan_sec_scope,
    resolve_amendment,
    transaction_record,
)
from iirp.models import (
    AmendmentRelation,
    Base,
    Batch,
    BatchJob,
    FeedRevision,
    Filing,
    FilingOwner,
    FilingVersion,
    Issuer,
    Job,
    Owner,
    RequestScope,
    SourceObject,
    SourceObservation,
    TransactionEvent,
)
from psycopg import sql
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url

FIXTURE = Path(__file__).parents[1] / "fixtures" / "synthetic_ownership_form4.xml"
ACCESSION = "0000000123-26-000001"
JOB = SimpleNamespace(id=None, target={})


@pytest.fixture(scope="module", autouse=True)
def isolated_database():
    url = make_url(settings().database_url)
    name = "iirp_v1_test_sec_" + uuid.uuid4().hex[:10]
    admin = psycopg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        dbname="postgres",
        autocommit=True,
    )
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    previous = os.environ.get("IIRP_DATABASE_URL")
    os.environ["IIRP_DATABASE_URL"] = url.set(database=name).render_as_string(hide_password=False)
    engine.cache_clear()
    settings.cache_clear()
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    yield
    engine().dispose()
    engine.cache_clear()
    settings.cache_clear()
    if previous is None:
        os.environ.pop("IIRP_DATABASE_URL", None)
    else:
        os.environ["IIRP_DATABASE_URL"] = previous
    admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
    admin.close()


@pytest.fixture(autouse=True)
def clean():
    with engine().begin() as connection:
        connection.execute(
            text(
                "TRUNCATE "
                + ",".join('"' + table.name + '"' for table in Base.metadata.sorted_tables)
                + " CASCADE"
            )
        )


def save(s, xml=None, accession=ACCESSION, accepted="2026-09-01T18:00:00-04:00"):
    xml = FIXTURE.read_bytes() if xml is None else xml
    digest = hashlib.sha256(xml).hexdigest()
    if s.get(SourceObject, digest) is None:
        s.add(
            SourceObject(
                sha256=digest,
                relative_path="synthetic/" + digest,
                byte_size=len(xml),
                media_type="application/xml",
            )
        )
        s.flush()
    document_url = (
        f"https://www.sec.gov/Archives/edgar/data/123/{accession.replace('-', '')}/ownership.xml"
    )
    form = "4/A" if b"<documentType>4/A</documentType>" in xml else "4"
    response = {
        "filing": {
            "accession": accession,
            "form": form,
            "accepted_at": accepted,
            "filing_date": "2026-09-01",
            "document_url": document_url,
            "xml_payload": xml.decode(),
            "xml_encoding": "utf8",
            "xml_sha256": digest,
        }
    }
    persist_document(s, JOB, response, {document_url: digest})
    return response


def test_joint_filings_store_all_owners_and_holdings_without_multiplying_amount():
    with session() as s, s.begin():
        save(s)
        assert s.scalar(select(func.count()).select_from(Owner)) == 2
        assert s.scalar(select(func.count()).select_from(FilingOwner)) == 2
        assert s.scalar(select(func.count()).select_from(TransactionEvent)) == 2
        version = s.scalar(select(FilingVersion))
        assert len(version.data["rows"]) == 3
        assert version.data["rows"][1]["row_kind"] == "holding"
        result = feed(s)
        group = result["groups"][0]
        assert group["owners"] == 2
        assert group["total_transactions"] == 2
        purchase = next(row for row in group["summary"] if row["code"] == "P")
        assert purchase["shares"] == "100000"
        assert purchase["known_amount"] == "2525000.00"


def test_repeated_source_commit_and_reparse_do_not_duplicate_facts():
    with session() as s, s.begin():
        save(s)
        original = list(s.scalars(select(TransactionEvent.id).order_by(TransactionEvent.id)))
        save(s)
        assert s.scalar(select(func.count()).select_from(FilingVersion)) == 1
        assert s.scalar(select(func.count()).select_from(SourceObservation)) == 1
        assert (
            list(s.scalars(select(TransactionEvent.id).order_by(TransactionEvent.id))) == original
        )
        assert s.scalar(select(func.count()).select_from(FeedRevision)) == 1


def test_source_revision_replaces_only_changed_row_and_preserves_old_snapshot():
    with session() as s, s.begin():
        save(s)
        before = feed(s)
        original_rows = {
            event.data["code"]: event.id for event in s.scalars(select(TransactionEvent))
        }
    with session() as s, s.begin():
        changed = FIXTURE.read_bytes().replace(b"<value>25.25</value>", b"<value>26.00</value>")
        save(s, changed)
        current = {
            event.data["code"]: event.id
            for event in s.scalars(
                select(TransactionEvent).where(TransactionEvent.status == "CURRENT")
            )
        }
        assert current["P"] != original_rows["P"]
        assert current["M"] == original_rows["M"]
        assert s.get(TransactionEvent, original_rows["P"]).status == "SUPERSEDED"
        frozen = feed(s, session_id=before["session_id"])
        fresh = feed(s)
        assert (
            next(row for row in frozen["groups"][0]["transactions"] if row["code"] == "P")["price"]
            == "25.25"
        )
        assert (
            next(row for row in fresh["groups"][0]["transactions"] if row["code"] == "P")["price"]
            == "26.00"
        )
        assert s.scalar(select(func.count()).select_from(FilingVersion)) == 2


def test_inserting_a_holding_before_transaction_keeps_unchanged_event_identity():
    with session() as s, s.begin():
        save(s)
        original_ids = set(s.scalars(select(TransactionEvent.id)))
        changed = FIXTURE.read_bytes().replace(
            b"<nonDerivativeTable>",
            b"<nonDerivativeTable><nonDerivativeHolding><securityTitle><value>New synthetic holding</value></securityTitle></nonDerivativeHolding>",
        )
        save(s, changed)
        assert set(s.scalars(select(TransactionEvent.id))) == original_ids
        assert set(s.scalars(select(TransactionEvent.status))) == {"CURRENT"}
        version = s.get(FilingVersion, s.get(Filing, ACCESSION).current_version)
        assert "I:transaction:2" in version.data["event_rows"]


def test_amendment_stays_uncertain_until_documented_row_level_resolution():
    with session() as s, s.begin():
        save(s)
        original = s.scalar(
            select(TransactionEvent).where(TransactionEvent.data["code"].astext == "P")
        )
        amended = (
            FIXTURE.read_bytes()
            .replace(b"<documentType>4</documentType>", b"<documentType>4/A</documentType>")
            .replace(b"<value>25.25</value>", b"<value>26.00</value>")
        )
        save(s, amended, "0000000123-26-000002", "2026-09-02T18:00:00-04:00")
        relations = list(
            s.scalars(select(AmendmentRelation).where(AmendmentRelation.action == "UNCONFIRMED"))
        )
        assert len(relations) == 2
        relation = next(
            item
            for item in relations
            if s.get(TransactionEvent, item.amended_event_id).data["code"] == "P"
        )
        assert s.get(TransactionEvent, relation.amended_event_id).status == "NEEDS_REVIEW"
        result = resolve_amendment(
            s,
            relation.id,
            "replace",
            original.id,
            {"note": "SYNTHETIC row correction explicitly confirmed from fixture."},
        )
        assert result["action"] == "REPLACE"
        amended_event = s.get(TransactionEvent, relation.amended_event_id)
        assert amended_event.accepted_at.isoformat() == "2026-09-02T22:00:00+00:00"
        assert s.get(TransactionEvent, original.id).status == "SUPERSEDED"
        groups = feed(s)["groups"]
        old_group = next(group for group in groups if group["accepted_date"] == "2026-09-01")
        new_group = next(group for group in groups if group["accepted_date"] == "2026-09-02")
        assert (
            next(row for row in old_group["summary"] if row["code"] == "P")["known_amount"]
            == "2600000.00"
        )
        assert (
            next(row for row in new_group["summary"] if row["code"] == "P")["known_amount"] is None
        )
        assert any(row.get("is_amendment_update") for row in new_group["transactions"])
        assert s.scalar(select(func.count()).select_from(FilingVersion)) == 2


def test_distinct_accession_identical_economics_remain_visible_and_uncertain():
    with session() as s, s.begin():
        save(s)
        save(s, accession="0000000123-26-000002")
        assert s.scalar(select(func.count()).select_from(TransactionEvent)) == 4
        assert (
            s.scalar(
                select(func.count())
                .select_from(TransactionEvent)
                .where(TransactionEvent.status == "CURRENT")
            )
            == 0
        )
        assert (
            s.scalar(
                select(func.count())
                .select_from(AmendmentRelation)
                .where(AmendmentRelation.action == "UNCONFIRMED_DUPLICATE")
            )
            == 2
        )
        assert feed(s)["groups"][0]["total_transactions"] == 4


def test_owner_history_keeps_joint_row_single_and_freezes_filter_scope():
    with session() as s, s.begin():
        save(s)
        result = entity_history(s, "owner", "456", recent_count=50, limit=1)["data"]
        assert result["total"] == 2
        assert not result["coverage"]["complete"]
        continued = entity_history(
            s,
            "owner",
            "456",
            recent_count=50,
            limit=1,
            cursor=result["next_cursor"],
            session_id=result["session_id"],
        )["data"]
        assert continued["items"][0]["id"] != result["items"][0]["id"]
        with pytest.raises(ValueError, match="条件"):
            entity_history(s, "owner", "789", recent_count=50, session_id=result["session_id"])


def test_missing_acceptance_time_is_visible_in_entity_history_without_fake_feed_time():
    with session() as s, s.begin():
        save(s, accepted=None)
        result = feed(s)
        assert not result["groups"]
        assert result["coverage"]["missing_acceptance_rows"] == 2
        history = entity_history(s, "company", "123")["data"]
        assert history["total"] == 2
        assert all(row["accepted_at"] is None for row in history["items"])


def test_read_detail_exposes_original_evidence_and_all_owners():
    with session() as s, s.begin():
        save(s)
        event = s.scalar(
            select(TransactionEvent).where(TransactionEvent.data["code"].astext == "P")
        )
        record = transaction_record(s, event.id)
        assert len(record["owner_observations"]) == 2
        assert record["source_url"].endswith("ownership.xml")
        assert record["source_hash"]
        assert record["security_id"] is None


def test_caller_rollback_rolls_back_all_business_publication():
    with session() as s:
        save(s)
        s.rollback()
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Filing)) == 0
        assert s.scalar(select(func.count()).select_from(TransactionEvent)) == 0
        assert s.scalar(select(func.count()).select_from(FeedRevision)) == 0


def test_discovery_index_cik_is_not_assumed_to_be_the_issuer():
    digest = hashlib.sha256(b"SYNTHETIC index source").hexdigest()
    entry = {
        "accession": ACCESSION,
        "form": "4",
        "entity_cik": "0000000456",
        "issuer_cik": None,
        "filing_date": "2026-09-01",
        "accepted_at": None,
        "index_url": f"https://www.sec.gov/Archives/edgar/data/456/{ACCESSION.replace('-', '')}/{ACCESSION}-index.html",
    }
    with session() as s, s.begin():
        s.add(
            SourceObject(
                sha256=digest,
                relative_path="synthetic/" + digest,
                byte_size=22,
                media_type="text/plain",
            )
        )
        s.flush()
        response = {"entries": [entry], "scan": {"complete": False, "reason": "page_budget"}}
        assert persist_discovery(
            s,
            JOB,
            response,
            {"https://www.sec.gov/Archives/edgar/full-index/2026/QTR3/master.idx": digest},
        ) == [entry]
        assert s.get(Filing, ACCESSION).issuer_id is None
        assert s.scalar(select(func.count()).select_from(Issuer)) == 0


def test_group_detail_uses_same_snapshot_and_filter_as_summary():
    with session() as s, s.begin():
        save(s)
        result = feed(s, kind="buy")
        group = result["groups"][0]
        assert group["matching_transactions"] == 1
        assert group["total_transactions"] == 2
        detail = feed_group(s, result["session_id"], group["id"])
        assert detail["items"] == group["transactions"]


def scope_fixture(s, *, request="scope-a", kind="sec_history"):
    from datetime import date

    batch = Batch(
        request_id=request, scope_key=request, kind=kind, title="SYNTHETIC scope", params={}
    )
    s.add(batch)
    s.flush()
    scope = RequestScope(
        batch_id=batch.id, symbol="SEC", start_date=date(2026, 9, 1), end_date=date(2026, 9, 4)
    )
    s.add(scope)
    s.flush()
    return batch, scope


def finish_index(s, job, entries):
    source_url = "https://www.sec.gov/Archives/edgar/full-index/2026/QTR3/master.idx"
    digest = hashlib.sha256((job.id + " synthetic index").encode()).hexdigest()
    s.add(
        SourceObject(
            sha256=digest,
            relative_path="synthetic/" + digest,
            byte_size=40,
            media_type="text/plain",
        )
    )
    s.flush()
    response = {
        "entries": entries,
        "cursor": None,
        "scan": {
            "mode": "quarterly",
            "complete": True,
            "reason": "requested_indexes_scanned",
            "start_date": "2026-07-01",
            "end_date": "2026-09-04",
            "scanned_indexes": [{"quarter": "2026-Q3", "as_of": "2026-09-04"}],
        },
    }
    persist_discovery(s, job, response, {source_url: digest})
    job.status = "SUCCEEDED"
    s.flush()


def discovery_entry(accession):
    return {
        "accession": accession,
        "form": "4",
        "filing_date": "2026-09-01",
        "accepted_at": None,
        "index_url": f"https://www.sec.gov/Archives/edgar/data/123/{accession.replace('-', '')}/{accession}-index.html",
    }


def test_scope_discovery_alone_cannot_succeed_and_rolls_documents_with_capacity():
    with session() as s, s.begin():
        batch, scope = scope_fixture(s)
        capacity = [1]
        plan_sec_scope(s, scope, batch, capacity)
        assert capacity == [0]
        discovery = s.scalar(select(Job))
        finish_index(
            s, discovery, [discovery_entry(ACCESSION), discovery_entry("0000000123-26-000002")]
        )
        plan_sec_scope(s, scope, batch, [0])
        assert scope.status == "QUEUED"
        assert scope.checkpoint["remaining_documents"] == 2
        plan_sec_scope(s, scope, batch, [1])
        assert scope.status == "RUNNING"
        assert (
            s.scalar(select(func.count()).select_from(Job).where(Job.kind == "sec_document")) == 1
        )
        assert scope.checkpoint["unscheduled_documents"] == 1


def test_scope_document_work_shares_across_batches_and_preserves_failures():
    with session() as s, s.begin():
        first, a = scope_fixture(s)
        plan_sec_scope(s, a, first, [2])
        finish_index(s, s.scalar(select(Job)), [discovery_entry(ACCESSION)])
        plan_sec_scope(s, a, first, [1])
        document = s.scalar(select(Job).where(Job.kind == "sec_document"))
        second, b = scope_fixture(s, request="scope-b")
        plan_sec_scope(s, b, second, [0])
        assert s.get(BatchJob, (b.id, document.id)) is not None
        assert (
            s.scalar(select(func.count()).select_from(Job).where(Job.kind == "sec_document")) == 1
        )
        document.status, document.error = "FAILED", "SYNTHETIC corrupt XML"
        plan_sec_scope(s, a, first, [5])
        assert a.status == "PARTIAL"
        assert "corrupt XML" in a.wait_reason
        assert document.status == "FAILED"


def test_scope_ready_requires_the_manifest_and_all_documents_parsed():
    with session() as s, s.begin():
        batch, scope = scope_fixture(s)
        plan_sec_scope(s, scope, batch, [2])
        finish_index(s, s.scalar(select(Job)), [discovery_entry(ACCESSION)])
        plan_sec_scope(s, scope, batch, [1])
        document = s.scalar(select(Job).where(Job.kind == "sec_document"))
        save(s)
        document.status = "SUCCEEDED"
        plan_sec_scope(s, scope, batch, [0])
        assert scope.status == "READY"
        assert scope.checkpoint["parsed_count"] == 1
        assert scope.checkpoint["scan_done"]


def test_paused_scope_creates_no_work_and_latest_discovery_is_not_a_job():
    with session() as s, s.begin():
        batch, scope = scope_fixture(s, kind="sec_latest")
        batch.status = "PAUSED"
        plan_sec_scope(s, scope, batch, [5])
        assert s.scalar(select(func.count()).select_from(Job)) == 0
        batch.status = "QUEUED"
        plan_sec_scope(s, scope, batch, [1])
        # The latest feed is polled from the source_poll row (iirp.sec.poll).
        assert s.scalar(select(func.count()).select_from(Job)) == 0


def test_quarterly_retraction_preserves_old_source_and_frozen_feed():
    with session() as s, s.begin():
        save(s)
        old = feed(s)
        batch, scope = scope_fixture(s)
        plan_sec_scope(s, scope, batch, [1])
        finish_index(s, s.scalar(select(Job)), [])
        filing = s.get(Filing, ACCESSION)
        assert not filing.visible
        assert filing.status == "SOURCE_WITHDRAWN"
        assert s.scalar(select(func.count()).select_from(FilingVersion)) == 1
        assert not feed(s)["groups"]
        assert feed(s, session_id=old["session_id"])["groups"]
        persist_discovery(s, JOB, {"entries": [discovery_entry(ACCESSION)], "scan": {}}, {})
        assert feed(s)["groups"]
        assert s.get(Filing, ACCESSION).visible


def test_weekly_refresh_rechecks_same_accession_once_per_week_without_retry_loop():
    from datetime import datetime, timezone

    with session() as s, s.begin():
        save(s)
        first_version = s.get(Filing, ACCESSION).current_version
        batch, scope = scope_fixture(s)
        batch.params = {"intent": "refresh"}
        batch.created_at = datetime(2026, 9, 7, tzinfo=timezone.utc)
        plan_sec_scope(s, scope, batch, [1])
        index = s.scalar(select(Job).where(Job.kind == "sec_discover"))
        assert index.target["reconcile_documents"]
        finish_index(s, index, [discovery_entry(ACCESSION)])
        plan_sec_scope(s, scope, batch, [1])
        document = s.scalar(select(Job).where(Job.kind == "sec_document"))
        assert document.target["reconcile_round"] == "2026-W36"  # Sept 6 in US/Eastern.
        for _ in range(3):
            plan_sec_scope(s, scope, batch, [5])
        assert s.scalar(select(func.count()).select_from(Job)) == 2
        changed = FIXTURE.read_bytes().replace(b"<value>25.25</value>", b"<value>26.00</value>")
        save(s, changed)
        document.status = "SUCCEEDED"
        plan_sec_scope(s, scope, batch, [0])
        assert scope.status == "READY" and scope.checkpoint["remaining_rechecks"] == 0
        assert s.get(Filing, ACCESSION).current_version != first_version
        assert s.scalar(select(func.count()).select_from(FilingVersion)) == 2
        next_batch, next_scope = scope_fixture(s, request="next-week")
        next_batch.params = {"intent": "refresh"}
        next_batch.created_at = datetime(2026, 9, 14, tzinfo=timezone.utc)
        plan_sec_scope(s, next_scope, next_batch, [1])
        next_index = s.scalar(
            select(Job)
            .join(BatchJob)
            .where(BatchJob.scope_id == next_scope.id, Job.kind == "sec_discover")
        )
        finish_index(s, next_index, [discovery_entry(ACCESSION)])
        plan_sec_scope(s, next_scope, next_batch, [1])
        next_document = s.scalar(
            select(Job)
            .join(BatchJob)
            .where(BatchJob.scope_id == next_scope.id, Job.kind == "sec_document")
        )
        assert next_document.id != document.id
        next_document.status, next_document.error = "FAILED", "SYNTHETIC rate limit"
        for _ in range(3):
            plan_sec_scope(s, next_scope, next_batch, [5])
        assert next_scope.status == "PARTIAL"
        assert s.scalar(select(func.count()).select_from(Job)) == 4
        assert s.get(Filing, ACCESSION).current_version != first_version


def test_refresh_schedules_first_documents_before_known_filing_rechecks():
    # An active weekly refresh may include newer parsed and older DISCOVERED
    # filings. One bounded document slot must advance coverage first; the
    # parsed filing remains eligible for a later source revision check.
    missing_accession = "0000000123-26-000003"
    with session() as s, s.begin():
        save(s)
        batch, scope = scope_fixture(s, request="refresh-first-documents")
        batch.params = {"intent": "refresh"}
        plan_sec_scope(s, scope, batch, [1])
        index = s.scalar(select(Job).where(Job.kind == "sec_discover"))
        finish_index(s, index, [
            discovery_entry(ACCESSION),
            discovery_entry(missing_accession),
        ])
        assert s.get(Filing, ACCESSION).current_version is not None
        assert s.get(Filing, missing_accession).current_version is None
        plan_sec_scope(s, scope, batch, [1])
        docs = list(s.scalars(select(Job).where(Job.kind == "sec_document")))
        assert len(docs) == 1
        assert docs[0].target["accession"] == missing_accession
        assert docs[0].target.get("reconcile_round")
        assert scope.checkpoint["remaining_documents"] == 1
        assert scope.checkpoint["remaining_rechecks"] == 1


def test_refresh_reserves_bounded_recheck_while_first_downloads_advance():
    # A large DISCOVERED backlog must not postpone all source revision checks.
    # With eight slots, the first seven go to initial downloads and one to
    # the already parsed filing; later planning resumes the remaining backlog.
    with session() as s, s.begin():
        save(s)
        batch, scope = scope_fixture(s, request="refresh-bounded-rechecks")
        batch.params = {"intent": "refresh"}
        plan_sec_scope(s, scope, batch, [1])
        index = s.scalar(select(Job).where(Job.kind == "sec_discover"))
        pending = [discovery_entry(f"0000000123-26-{number:06d}")
                   for number in range(20, 34)]
        finish_index(s, index, [discovery_entry(ACCESSION), *pending])
        plan_sec_scope(s, scope, batch, [8])
        docs = list(s.scalars(select(Job).where(Job.kind == "sec_document")))
        assert len(docs) == 8
        accessions = {doc.target["accession"] for doc in docs}
        assert ACCESSION in accessions
        assert len(accessions - {ACCESSION}) == 7
        assert scope.checkpoint["remaining_documents"] == 14
        assert scope.checkpoint["remaining_rechecks"] == 1
        plan_sec_scope(s, scope, batch, [8])
        continued = list(s.scalars(select(Job).where(Job.kind == "sec_document")))
        assert len(continued) == 15
        assert {doc.target["accession"] for doc in continued} == (
            {ACCESSION} | {entry["accession"] for entry in pending}
        )
        assert scope.checkpoint["remaining_documents"] == 14
        assert scope.checkpoint["remaining_rechecks"] == 1


def test_facts_reference_the_saved_filing_instead_of_copying_its_xml():
    from iirp.insider.facts import transaction_record
    from iirp.models import FilingVersion, SourceObject, TransactionEvent

    with session() as s, s.begin():
        save(s)
        group = feed(s)["groups"][0]
        row = group["transactions"][0]
        assert "raw_xml" not in row and "footnotes" not in row
        detail = transaction_record(s, row["id"])
        assert "raw_xml" not in detail and "footnotes" in detail
        assert detail["xml_sha256"] and detail["source_documents"]
        assert all(s.get(SourceObject, doc["sha256"]) for doc in detail["source_documents"])
        assert all("raw_xml" not in event.data for event in s.scalars(select(TransactionEvent)))
        for version in s.scalars(select(FilingVersion)):
            assert "raw_xml" not in version.data
            assert "xml_payload" not in version.data["source_metadata"]
            assert all("raw_xml" not in item for item in version.data["rows"])
        history = entity_history(s, "company", "123")
        assert all("raw_xml" not in item and "footnotes" not in item for item in history["items"])
