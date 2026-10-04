"""Real PostgreSQL phase-one query and derived-manifest lifecycle regressions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from time import monotonic

import pytest
from alembic import command
from alembic.config import Config
from iirp.business_models import (
    Batch,
    FeedManifest,
    FeedRevision,
    FeedSession,
    Filing,
    FilingVersion,
    RequestScope,
    TransactionEvent,
)
from iirp.config import ROOT, settings
from iirp.db import engine, session
from iirp.feed_snapshots import (
    MANIFEST_CLEANUP_PAGE,
    cleanup_feed_manifests,
    freeze_feed_session,
    session_revision_ids,
)
from iirp.feed_updates import pending_feed_metadata
from iirp.lifecycle import add_job
from iirp.maintenance import cleanup
from iirp.models import Job, SourceObject, now
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_sec_facts import clean, isolated_database, save  # noqa: F401


def test_pending_uses_latest_attempt_tiebreak_and_excludes_parsed_invisible():
    instant = now()
    with session() as s, s.begin():
        for accession, visible, parsed in [("pending", True, None), ("no-job", True, None),
                                          ("parsed", True, "version"), ("hidden", False, None)]:
            s.add(Filing(accession=accession, form="4", visible=visible, current_version=parsed))
        for identifier, status in [("a", "FAILED"), ("z", "PAUSE_REQUESTED")]:
            s.add(Job(id=identifier, kind="sec_document", title="synthetic", status=status,
                      target={"accession": "pending"}, idempotency_key=identifier, created_at=instant))
        s.add(Job(kind="other", title="not a document", status="SUCCEEDED",
                  target={"accession": "pending"}, idempotency_key="other"))
        s.flush()
        summary, preview = pending_feed_metadata(s, preview=True)
        assert summary["total"] == 2
        assert {row["accession"]: row["stage"] for row in preview} == {
            "pending": "pause_requested", "no-job": "discovered"}


def test_history_key_reuses_terminal_and_preserves_active_uniqueness():
    with session() as s, s.begin():
        batch = Batch(request_id="history-test", scope_key="history-test", kind="sec_history",
                      title="synthetic", params={})
        s.add(batch)
        s.flush()
        scope = RequestScope(batch_id=batch.id, symbol="SYNTH", status="QUEUED")
        s.add(scope)
        s.flush()
        first = add_job(s, scope, "sec_document", {"accession": "history-test"})
        first.status = "SUCCEEDED"
        s.flush()
        assert add_job(s, scope, "sec_document", {"accession": "history-test"}).id == first.id
        latest = Job(kind=first.kind, title="later terminal attempt", target=first.target,
                     idempotency_key=first.idempotency_key, status="SUCCEEDED",
                     created_at=now() + timedelta(seconds=1))
        s.add(latest)
        s.flush()
        assert add_job(s, scope, "sec_document", {"accession": "history-test"}).id == latest.id
        s.add(Job(kind=first.kind, title="active", target=first.target,
                  idempotency_key=first.idempotency_key, status="QUEUED"))
        s.flush()
        active = add_job(s, scope, "sec_document", {"accession": "history-test"})
        assert active.status == "QUEUED" and active.id != first.id
        with pytest.raises(IntegrityError), s.begin_nested():
            s.add(Job(kind=first.kind, title="duplicate", target=first.target,
                      idempotency_key=first.idempotency_key, status="QUEUED"))
            s.flush()
        assert s.scalar(select(func.count()).select_from(Job)) == 3


def test_gc_bounds_grace_and_all_existing_references():
    old = now() - timedelta(days=2)
    with session() as s, s.begin():
        active = freeze_feed_session(s, ["active"], {})
        expired = freeze_feed_session(s, ["expired-reference"], {})
        expired.expires_at = old
        for saved in (active, expired):
            s.get(FeedManifest, saved.manifest_hash).created_at = old
        for index in range(MANIFEST_CLEANUP_PAGE + 2):
            s.add(FeedManifest(sha256=f"orphan-{index}", revision_ids=[f"rev-{index}"], created_at=old))
        s.add(FeedManifest(sha256="recent", revision_ids=["recent"]))
    with session() as s, s.begin():
        assert cleanup_feed_manifests(s)["feed_manifests_removed"] == MANIFEST_CLEANUP_PAGE
        assert session_revision_ids(s, s.get(FeedSession, active.id)) == ["active"]
        assert session_revision_ids(s, s.get(FeedSession, expired.id)) == ["expired-reference"]
        assert s.get(FeedManifest, "recent") is not None
    with session() as s, s.begin():
        assert cleanup_feed_manifests(s)["feed_manifests_removed"] == 2
        s.execute(delete(FeedSession).where(FeedSession.id == expired.id))
    with session() as s, s.begin():
        assert cleanup_feed_manifests(s)["feed_manifests_removed"] == 1
        assert s.get(FeedManifest, active.manifest_hash) is not None


def test_gc_defers_for_uncommitted_publisher_and_preserves_new_reference():
    with session() as s, s.begin():
        saved = freeze_feed_session(s, ["race"], {})
        digest = saved.manifest_hash
        s.get(FeedManifest, digest).created_at = now() - timedelta(days=2)
        s.delete(saved)
    started, release = Event(), Event()

    def publish():
        with session() as s, s.begin():
            saved = freeze_feed_session(s, ["race"], {})
            started.set()
            assert release.wait(5)
            return saved.id

    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(publish)
        try:
            assert started.wait(5)
            with session() as s, s.begin():
                assert cleanup_feed_manifests(s) == {
                    "feed_manifests_removed": 0, "feed_manifest_cleanup_busy": True}
        finally:
            release.set()
        identifier = task.result(timeout=5)
    with session() as s, s.begin():
        assert cleanup_feed_manifests(s)["feed_manifests_removed"] == 0
        assert session_revision_ids(s, s.get(FeedSession, identifier)) == ["race"]


def test_maintenance_entry_reclaims_derived_indexes_and_keeps_source_facts(tmp_path, monkeypatch):
    monkeypatch.setattr(settings(), "runtime_dir", tmp_path)
    facts = (Filing, FilingVersion, FeedRevision, TransactionEvent, SourceObject)
    with session() as s, s.begin():
        assert s.scalar(text("SELECT current_database()")).startswith("iirp_v1_test_")
        save(s)
        expired = freeze_feed_session(s, ["expired"], {})
        expired.expires_at = now() - timedelta(seconds=1)
        s.get(FeedManifest, expired.manifest_hash).created_at = now() - timedelta(days=2)
        live = freeze_feed_session(s, ["live"], {})
        s.get(FeedManifest, live.manifest_hash).created_at = now() - timedelta(days=2)
        s.add(FeedManifest(sha256="old-orphan", revision_ids=["orphan"], created_at=now() - timedelta(days=2)))
        s.add(FeedManifest(sha256="recent-orphan", revision_ids=["recent"]))
        before = [s.scalar(select(func.count()).select_from(model)) for model in facts]
    details = cleanup()["data"]
    assert details["reading_sessions_removed"] == 1
    assert details["feed_manifests_removed"] == 2
    with session() as s:
        assert [s.scalar(select(func.count()).select_from(model)) for model in facts] == before
        assert session_revision_ids(s, s.get(FeedSession, live.id)) == ["live"]
        assert s.get(FeedManifest, "recent-orphan") is not None


def test_publisher_recreates_manifest_after_concurrent_gc_commits():
    with session() as s, s.begin():
        saved = freeze_feed_session(s, ["recreate"], {})
        digest = saved.manifest_hash
        s.get(FeedManifest, digest).created_at = now() - timedelta(days=2)
        s.delete(saved)
    entered = Event()

    def publish():
        with session() as s, s.begin():
            s.execute(text("SET LOCAL application_name = 'phase1-manifest-publisher'"))
            entered.set()
            return freeze_feed_session(s, ["recreate"], {}).id

    with ThreadPoolExecutor(max_workers=1) as pool:
        with session() as s, s.begin():
            assert cleanup_feed_manifests(s)["feed_manifests_removed"] == 1
            task = pool.submit(publish)
            assert entered.wait(5)
            # Confirm the publisher is waiting for the GC transaction, then
            # release it. No sleeps and no changes to any live worker session.
            deadline = monotonic() + 3
            waiting = False
            while monotonic() < deadline:
                # pg_stat_activity snapshots are cleared between observations.
                s.execute(text("SELECT pg_stat_clear_snapshot()"))
                waiting = s.scalar(text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname=current_database() "
                    "AND application_name='phase1-manifest-publisher' AND wait_event='advisory')"))
                if waiting:
                    break
                Event().wait(.005)
            assert waiting, "publisher must wait until GC commits before publishing its reference"
        identifier = task.result(timeout=5)
    with session() as s:
        saved = s.get(FeedSession, identifier)
        assert saved.manifest_hash == digest
        assert session_revision_ids(s, saved) == ["recreate"]


def test_concurrent_index_failure_is_repairable_without_losing_facts():
    configuration = Config(str(ROOT / "alembic.ini"))
    with session() as s, s.begin():
        s.add(Job(id="migration-fact", kind="sec_document", title="synthetic", target={},
                  status="SUCCEEDED", idempotency_key="migration-fact"))
    command.downgrade(configuration, "0015")
    try:
        with engine().connect() as writer:
            assert writer.scalar(text("SELECT current_database()" )).startswith("iirp_v1_test_")
            writer.execute(text("UPDATE job SET updated_at=updated_at WHERE id='migration-fact'"))
            with engine().connect().execution_options(isolation_level="AUTOCOMMIT") as builder:
                builder.execute(text("SET lock_timeout = '2s'"))
                builder.execute(text("SET statement_timeout = '1s'"))
                with pytest.raises(DBAPIError) as failure:
                    builder.execute(text(
                        "CREATE INDEX CONCURRENTLY ix_job_history_key ON job (idempotency_key,created_at DESC)"))
                assert failure.value.orig.sqlstate == "57014"
                assert builder.scalar(text(
                    "SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass('ix_job_history_key')")) is False
                builder.execute(text("SET statement_timeout = '5s'"))
                builder.execute(text("SET lock_timeout = '500ms'"))
            writer.rollback()
    finally:
        command.upgrade(configuration, "head")
    with session() as s:
        assert s.get(Job, "migration-fact").status == "SUCCEEDED"
        assert s.scalar(text(
            "SELECT indisvalid FROM pg_index WHERE indexrelid='ix_job_history_key'::regclass")) is True
