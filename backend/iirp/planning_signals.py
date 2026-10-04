"""Transactional outbox: completion and its follow-up commit or roll back together."""
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from iirp.business_models import Batch, BatchJob, BatchPlanSignal, RequestScope
from iirp.models import Job, now


def latest_sec_demand(s):
    return s.scalar(select(Batch.id).where(Batch.kind == "sec_latest", Batch.trigger == "automatic",
        Batch.requested_action.is_(None), Batch.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT", "PARTIAL")))
        .order_by(Batch.created_at.desc()).limit(1))


def signal_batch(s, batch_id):
    s.execute(insert(BatchPlanSignal).values(batch_id=batch_id, token=str(uuid4()), updated_at=now())
        .on_conflict_do_update(index_elements=[BatchPlanSignal.batch_id],
            set_={"token": str(uuid4()), "updated_at": now()}))


def signal_job(s, job_id):
    identifiers = set(s.scalars(select(RequestScope.batch_id)
        .join(BatchJob, BatchJob.scope_id == RequestScope.id)
        .where(BatchJob.job_id == job_id, BatchJob.active.is_(True)).distinct()))
    job = s.get(Job, job_id)
    if job and job.kind in ("sec_document", "sec_discover"):
        # Free capacity belongs first to the newest automatic demand. Otherwise
        # old batches can refill all eight slots while today's batch waits in rotation.
        newest = latest_sec_demand(s)
        if newest:
            identifiers.add(newest)
    if identifiers:
        stamp = now()
        rows = [{"batch_id": identifier, "token": str(uuid4()), "updated_at": stamp}
                for identifier in sorted(identifiers)]
        stmt = insert(BatchPlanSignal).values(rows)
        s.execute(stmt.on_conflict_do_update(index_elements=[BatchPlanSignal.batch_id],
            set_={"token": stmt.excluded.token, "updated_at": stmt.excluded.updated_at}))
