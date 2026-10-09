"""Plan one company's or person's recent filings from SEC (Insider lookup).

EDGAR's submissions file of a CIK lists the Form 3/4/5 filed for it as issuer
or reporting owner. One discovery reads it (plus older files when the range
reaches them); each listed filing then goes through the usual document job.
Only that entity's filings are fetched, never the whole market's index.
"""

from sqlalchemy import select

from iirp.insider.common import ET, _json
from iirp.messages import msg
from iirp.models import ACTIVE, Filing, Job


def plan_entity_scope(s, scope, batch, capacity: list[int]) -> None:
    from iirp.jobs.batches import add_job

    if batch.requested_action or batch.status in {"PAUSED", "CANCELLED", "PAUSE_REQUESTED", "CANCEL_REQUESTED"}:
        return
    target = {
        "mode": "entity",
        "cik": batch.params["cik"],
        "start_date": str(scope.start_date),
        "end_date": str(scope.end_date),
        # The same entity and range read again on a later day is new work.
        "round": batch.created_at.astimezone(ET).date().isoformat(),
    }
    discovery = add_job(s, scope, "sec_discover", target)
    if discovery.status in ACTIVE:
        scope.status, scope.wait_reason = "RUNNING", discovery.error
        return
    accessions = discovery.checkpoint.get("discovered_accessions") or []
    if discovery.status != "SUCCEEDED" and not accessions:
        scope.status, scope.wait_reason = "PARTIAL", discovery.error or msg("sec.scope.gaps")
        return
    filings = s.scalars(
        select(Filing).where(Filing.accession.in_(accessions), Filing.visible.is_(True))
        .order_by(Filing.accepted_at.desc().nulls_last(), Filing.accession.desc())
    ).all()
    jobs, unscheduled = [], 0
    for filing in filings:
        if filing.current_version:
            continue
        shared = s.scalar(select(Job).where(
            Job.kind == "sec_document", Job.target["accession"].astext == filing.accession,
        ).order_by(Job.created_at.desc()).limit(1))
        if shared is not None:
            jobs.append(add_job(s, scope, shared.kind, shared.target))
            continue
        if capacity[0] <= 0:
            unscheduled += 1
            continue
        document = {
            "accession": filing.accession,
            "form": filing.form,
            "index_url": filing.index_url,
            "filing_date": _json(filing.filing_date),
            "accepted_at": _json(filing.accepted_at),
        }
        if filing.index_url.endswith(("-index.html", "-index.htm")):
            document["submission_url"] = filing.index_url.rsplit("-index.", 1)[0] + ".txt"
        jobs.append(add_job(s, scope, "sec_document", document))
        capacity[0] -= 1
    parsed = sum(1 for filing in filings if filing.current_version)
    scope.checkpoint = {
        **(scope.checkpoint or {}),
        "discovered_count": len(filings),
        "parsed_count": parsed,
        "remaining_documents": len(filings) - parsed,
        "entity": discovery.result.get("entity") if discovery.result else None,
    }
    active = [job for job in jobs if job.status in ACTIVE]
    failed = [job for job in jobs if job.status in {"FAILED", "PARTIAL", "CANCELLED"}]
    if active or unscheduled:
        scope.status = "RUNNING" if active else "QUEUED"
        scope.wait_reason = None if active else msg("sec.scope.continuing")
    elif failed or discovery.status != "SUCCEEDED":
        scope.status = "PARTIAL"
        scope.wait_reason = next((job.error for job in failed if job.error), discovery.error or msg("sec.scope.gaps"))
    else:
        scope.status, scope.wait_reason = "READY", None
