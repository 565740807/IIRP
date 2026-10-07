"""Insider lookup: companies by ticker or name, people by name (D15).

Local data answers first. A query not found locally goes to SEC through a
durable job (the API never waits on a source): a ticker is matched in SEC's
company ticker list, a name in EDGAR's company and person search. Choosing a
result fetches that entity's filings of the chosen range (``sec_entity``).
"""

import hashlib
import json
import re
from datetime import date

from dateutil.relativedelta import relativedelta
from sqlalchemy import and_, func, or_, select

from iirp.insider.common import ET, _cik
from iirp.messages import NotFoundError, UserError
from iirp.models import Batch, Issuer, Job, Owner, TransactionEvent, now

TICKERISH = re.compile(r"[A-Za-z][A-Za-z0-9.\-]{0,9}")
LIMIT = 10


def _words(query: str) -> list[str]:
    return [word for word in re.split(r"[\s,]+", query) if word][:6]


def _like(word: str) -> str:
    return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def local_lookup(s, query: str) -> dict:
    """Saved companies (ticker exact or every word in the name) and people (every word)."""
    query = query.strip()
    if not query:
        return {"companies": [], "people": []}
    if len(query) > 100:
        raise UserError("search.too_long", max=100)
    words = _words(query)
    by_name = [Issuer.name.ilike(_like(word)) for word in words]
    conditions = [func.upper(Issuer.ticker) == query.upper(), and_(*by_name)]
    if query.isdigit() and len(query) <= 10:
        conditions.append(Issuer.id == query.zfill(10))
    companies = s.scalars(
        select(Issuer).where(or_(*conditions))
        .order_by((func.upper(Issuer.ticker) == query.upper()).desc(), Issuer.name).limit(LIMIT)
    ).all()
    people = s.scalars(
        select(Owner).where(and_(*[Owner.name.ilike(_like(word)) for word in words])).order_by(Owner.name).limit(LIMIT)
    ).all()
    last_company = _last_trades(s, [row.id for row in companies], "issuer")
    last_person = _last_trades(s, [row.id for row in people], "owner")
    return {
        "companies": [{"cik": row.id, "name": row.name, "ticker": row.ticker,
                       "last_trade": last_company.get(row.id)} for row in companies],
        "people": [{"cik": row.id, "name": row.name, "last_trade": last_person.get(row.id)} for row in people],
    }


def _last_trades(s, ids, kind):
    """Most recent transaction date per entity, to tell active results apart."""
    if not ids:
        return {}
    if kind == "issuer":
        rows = s.execute(select(TransactionEvent.issuer_id, func.max(TransactionEvent.transaction_date))
                         .where(TransactionEvent.issuer_id.in_(ids)).group_by(TransactionEvent.issuer_id))
        return {key: value.isoformat() if value else None for key, value in rows}
    result = {}
    for identifier in ids:
        value = s.scalar(select(func.max(TransactionEvent.transaction_date))
                         .where(TransactionEvent.owner_ids.contains([identifier])))
        result[identifier] = value.isoformat() if value else None
    return result


def _job_key(kind, target):
    return hashlib.sha256(json.dumps([kind, target], sort_keys=True).encode()).hexdigest()


def start_sec_lookup(query: str) -> Job:
    """One SEC lookup per query and day; the same query again reuses its result."""
    from iirp.db import session
    from iirp.jobs.queue import create_job

    query = " ".join(_words(query.strip()))
    if not query or len(query) > 100:
        raise UserError("search.too_long", max=100)
    mode = "ticker" if TICKERISH.fullmatch(query) else "name"
    target = {"query": query.upper() if mode == "ticker" else query, "mode": mode,
              "round": now().astimezone(ET).date().isoformat()}
    with session() as s:
        done = s.scalar(select(Job).where(Job.idempotency_key == _job_key("sec_identity", target),
                                          Job.status == "SUCCEEDED").limit(1))
        if done is not None:
            s.expunge(done)
            return done
    job, _ = create_job("sec_identity", target)
    return job


def lookup_result(s, job_id: str) -> dict:
    """Status of a SEC lookup and its candidates, marked when already saved locally."""
    job = s.get(Job, job_id)
    if job is None or job.kind != "sec_identity" or not job.target.get("query"):
        raise NotFoundError("job.not_found")
    candidates = (job.result or {}).get("candidates", []) if job.status == "SUCCEEDED" else []
    ciks = [row["cik"] for row in candidates]
    issuers = set(s.scalars(select(Issuer.id).where(Issuer.id.in_(ciks)))) if ciks else set()
    owners = set(s.scalars(select(Owner.id).where(Owner.id.in_(ciks)))) if ciks else set()
    return {
        "job_id": job.id,
        "status": job.status,
        "error": job.error,
        "query": job.target["query"],
        "candidates": [
            {**row, "kind": "company" if row.get("tickers") or row["cik"] in issuers else "person",
             "local": row["cik"] in issuers or row["cik"] in owners}
            for row in candidates
        ],
    }


def fetch_entity(cik: str, kind: str, name: str | None, *, months: int | None = None,
                 start_date: date | None = None) -> dict:
    """Fetch an entity's ownership filings since the chosen start (default 6 months)."""
    from iirp.db import session
    from iirp.jobs.batch_views import batch_view
    from iirp.jobs.batches import _create
    from iirp.jobs.providers import sec_configured

    if not sec_configured():
        raise UserError("sec.user_agent_required")
    cik = _cik(cik)
    if kind not in {"company", "person"}:
        raise UserError("insider.entity_kind_invalid")
    today = now().astimezone(ET).date()
    start = start_date or today - relativedelta(months=months or 6)
    if start > today:
        raise UserError("common.start_after_end")
    params = {"kind": "sec_entity", "cik": cik, "role": kind, "name": (name or "")[:200] or None,
              "start_date": start.isoformat(), "end_date": today.isoformat()}
    params["request_id"] = "sec-entity-" + hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:40]
    with session() as s, s.begin():
        batch, reused = _create(s, params)
        return {"batch_id": batch.id, "reused": reused, "batch": batch_view(s, batch)}


def entity_fetch_state(s, kind: str, cik: str) -> dict | None:
    """The latest SEC fetch for this entity, for the page's progress line."""
    batch = s.scalar(select(Batch).where(Batch.kind == "sec_entity", Batch.params["cik"].astext == _cik(cik))
                     .order_by(Batch.created_at.desc()).limit(1))
    if batch is None:
        return None
    from iirp.models import RequestScope

    scope = s.scalar(select(RequestScope).where(RequestScope.batch_id == batch.id).limit(1))
    checkpoint = scope.checkpoint if scope else {}
    return {"batch_id": batch.id, "status": batch.status, "created_at": batch.created_at.isoformat(),
            "start_date": batch.params.get("start_date"),
            "discovered": checkpoint.get("discovered_count"), "parsed": checkpoint.get("parsed_count"),
            "error": scope.wait_reason if scope else None}
