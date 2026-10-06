"""Real PostgreSQL phase-one query and derived-manifest lifecycle regressions."""

from datetime import timedelta

import pytest
from alembic import command
from alembic.config import Config
from iirp.config import ROOT
from iirp.db import engine, session
from iirp.insider.feed_updates import pending_feed_metadata
from iirp.jobs.lifecycle import add_job
from iirp.models import (
    Batch,
    Filing,
    Job,
    RequestScope,
    now,
)
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.sec.test_sec_facts import clean, isolated_database, save  # noqa: F401


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
