"""Per-stock sector references, with durable classification and local-only prices.

Choosing a reference never downloads prices. Long-term bars are materialized into
an expiring analysis input; an absent local ETF is reported explicitly. Identity
refreshes are scope-linked jobs, so analysis controls and completion fences apply.
"""

import re
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import object_session

from iirp.market.classification import classify
from iirp.messages import UserError, msg
from iirp.models import ACTIVE, Batch, BatchJob, Job, PriceCache, Security, now


class SectorReference(BaseModel):
    symbol: str | None = None
    status: str
    source: Literal["sp500_gics", "yahoo", "none", "custom"] = "none"
    mode: Literal["auto", "industry_group", "custom"] = "auto"
    sector_id: str | None = None
    industry_group_id: str | None = None
    fallback: bool = False
    reason: str | None = None


def validate_sector_benchmark(value):
    if value is None or not value.strip():
        return None
    value = value.strip()
    if value.lower() in {"auto", "industry_group"}:
        return value.lower()
    value = value.upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,19}", value):
        raise UserError("sector.invalid")
    return value


def sector_snapshot(params, stock, s=None, *, scope=None):
    choice = params.get("sector_benchmark")
    if not choice:
        return None
    s = s or object_session(stock)
    output = {"symbol": None, "status": "sector_unavailable", "source": "none",
              "mode": choice if choice in {"auto", "industry_group"} else "custom",
              "sector_id": None, "industry_group_id": None, "fallback": False,
              "reason": None, "security_id": None, "dataset_id": None}
    if choice in {"auto", "industry_group"}:
        mapping = classify(s, stock.symbol)
        sector, group = mapping["sector"], mapping["industry_group"]
        output.update(source=mapping["source"], sector_id=sector["id"] if sector else None,
                      industry_group_id=group["id"] if group else None)
        if mapping["refresh"] in {"needed", "pending"}:
            job_id = (scope.checkpoint or {}).get("sector_refresh_job") if scope else None
            job = s.get(Job, job_id) if job_id else None
            failed = mapping["refresh"] != "pending" and job and job.status not in ACTIVE
            output.update(status="sector_identity_failed" if failed else "sector_identity_pending",
                          reason=msg("sector.identity_failed") if failed else msg("sector.identity_pending"))
            return output
        if not sector:
            output["reason"] = msg("sector.unavailable")
            return output
        output["symbol"] = sector["etf"]
        if choice == "industry_group":
            if group and group["etf"]:
                output["symbol"] = group["etf"]
            else:
                output.update(fallback=True, reason=msg("sector.industry_group_fallback"))
    else:
        output.update(symbol=choice, source="custom")
    reference = s.scalar(select(Security).where(Security.symbol == output["symbol"])
                         .order_by(Security.id).limit(1))
    output["security_id"] = reference.id if reference else None
    if (not reference or reference.status != "VERIFIED" or reference.instrument != "ETF"
            or reference.calendar != stock.calendar or reference.currency != stock.currency):
        output.update(status="sector_reference_unavailable", reason=msg("sector.mismatch"))
        return output
    cache = s.scalar(select(PriceCache).where(PriceCache.security_id == reference.id,
                                             PriceCache.expires_at > now()))
    output.update(status="available" if cache else "sector_prices_unavailable",
                  dataset_id=cache.id if cache else None)
    if not cache:
        output["reason"] = msg("sector.prices_unavailable", symbol=reference.symbol)
    elif scope and scope.start_date and scope.end_date:
        from iirp.market.cache import covers
        if not covers(cache, scope.start_date, scope.end_date, reference.calendar):
            # Preserve usable later years (including ETFs listed partway through
            # the study) while making the missing requested history explicit.
            output["reason"] = msg("sector.prices_incomplete", symbol=reference.symbol)
    return output


def public_sector(snapshot):
    return SectorReference.model_validate(snapshot).model_dump() if snapshot else None


def plan_sector(s, scope, request, start, end):
    """Queue at most one classification refresh; use only existing local prices."""
    from iirp.jobs.batches import add_job, advisory
    from iirp.market.cache import covers
    from iirp.market.long_prices import local_series, materialize

    choice = request.params.get("sector_benchmark")
    if not choice:
        return None
    stock = s.get(Security, scope.security_id)
    if choice in {"auto", "industry_group"}:
        advisory(s, ["security", stock.symbol])
        mapping = classify(s, stock.symbol)
        if mapping["refresh"] in {"needed", "pending"}:
            saved_id = (scope.checkpoint or {}).get("sector_refresh_job")
            job = s.get(Job, saved_id) if saved_id else None
            active = s.scalar(select(Job).where(Job.kind == "market_identity", Job.status.in_(ACTIVE),
                Job.target["security_id"].astext == stock.id).order_by(Job.created_at).limit(1))
            if active is not None:
                job = active
            elif job is None:
                job = add_job(s, scope, "market_identity", {
                    "symbol": stock.symbol, "security_id": stock.id,
                    "classification_refresh": request.id})
            # Manual classification refreshes use a UUID work key. Link their
            # actual identity, rather than recomputing a different work key.
            link = s.get(BatchJob, (scope.id, job.id))
            if link is None:
                s.add(BatchJob(scope_id=scope.id, job_id=job.id, active=True))
            else:
                link.active = True
            batch = s.get(Batch, scope.batch_id)
            if batch:
                from iirp.jobs.batches import demand_priority
                job.priority = min(job.priority, demand_priority(batch))
            scope.checkpoint = {**scope.checkpoint, "sector_refresh_job": job.id}
            return msg("sector.identity_pending") if job.status in ACTIVE else msg("sector.identity_failed")
    snapshot = sector_snapshot(request.params, stock, s, scope=scope)
    if snapshot and snapshot.get("security_id"):
        reference = s.get(Security, snapshot["security_id"])
        cache = s.scalar(select(PriceCache).where(PriceCache.security_id == reference.id,
                                                 PriceCache.expires_at > now()))
        if snapshot["status"] in {"available", "sector_prices_unavailable"} and not covers(
                cache, start, end, reference.calendar):
            local = local_series(s, reference.id, start, end)
            if local is not None:
                materialize(s, local)
    return None
