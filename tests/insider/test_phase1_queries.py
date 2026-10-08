"""Real PostgreSQL phase-one query and derived-manifest lifecycle regressions."""

from datetime import timedelta

import pytest
from iirp.db import session
from iirp.insider.feed_updates import pending_feed_metadata
from iirp.jobs.batches import add_job
from iirp.models import (
    Batch,
    Filing,
    Job,
    RequestScope,
    now,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

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
