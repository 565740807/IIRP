"""24-hour daily price cache (D14).

Each security has at most one cached provider response. One request covers the
whole needed range plus a month of buffer, so every bar shares one adjustment
basis and nothing is versioned or stitched. Within 24 hours of the fetch every
analysis, chart and ±n-session window reuses it; a need outside the cached
range fetches the security again as one wider range. Expired caches and the
research results computed from them are deleted by the worker.
"""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from iirp.business_models import (
    AnalysisRequest,
    AnalysisResult,
    PriceCache,
    PriceCacheBar,
    RequestScope,
    Security,
)
from iirp.models import ACTIVE, Job, now


def _defaults():
    from iirp.profiles import profiles

    normal = profiles()["normal_usage"]
    return normal["price_cache_hours"], normal["price_buffer_days"]


CACHE_HOURS, BUFFER_DAYS = _defaults()
EXPIRY_BATCH = 200
BAR_FIELDS = ("open", "high", "low", "close", "adj_close", "volume")


def current_cache(s, security_id, *, at=None):
    """The unexpired cache of a security, or None."""
    return s.scalar(select(PriceCache).where(
        PriceCache.security_id == security_id, PriceCache.expires_at > (at or now())))


def cache_facts(cache):
    """Fetch time and expiry shown with every price-based reading."""
    if cache is None:
        return {"price_cache_id": None, "price_fetched_at": None, "price_expires_at": None,
                "price_expired": True}
    return {"price_cache_id": cache.id, "price_fetched_at": cache.fetched_at.isoformat(),
            "price_expires_at": cache.expires_at.isoformat(),
            "price_expired": cache.expires_at <= now()}


def price_bars(s, security_id, cache_id=None, *, ranges=None):
    """Bars of the given cache (or the current one) as plain strings."""
    cache = s.get(PriceCache, cache_id) if cache_id else current_cache(s, security_id)
    if cache is None or cache.security_id != security_id:
        return [], None
    query = select(PriceCacheBar).where(PriceCacheBar.cache_id == cache.id)
    if ranges is not None:
        from iirp.research_dependencies import range_predicate
        query = query.where(range_predicate(PriceCacheBar.session_date, ranges))
    rows = s.scalars(query.order_by(PriceCacheBar.session_date)).all()
    return [
        {
            "date": row.session_date.isoformat(),
            **{k: str(getattr(row, k)) if getattr(row, k) is not None else None for k in BAR_FIELDS},
            "status": row.status,
            "reason": row.reason,
        }
        for row in rows
    ], cache


def covers(cache, start, end, calendar="XNYS"):
    """Whether a cache answers [start, end]; sessions not yet closed are not needed."""
    from iirp.analytics.calendar import last_completed_session

    needed_end = min(end, last_completed_session(calendar=calendar))
    if needed_end < start:
        return True  # Only future sessions: nothing can be fetched yet.
    return cache is not None and cache.start_date <= start and cache.complete_through >= needed_end


def coverage_for(s, security, start, end, *, cache_id=None):
    from iirp.analytics.calendar import sessions

    if not security or security.status != "VERIFIED" or not security.calendar:
        return {
            "start_date": str(start),
            "end_date": str(end),
            "status": "IDENTITY_PENDING",
            "reasons": ["证券股类、币种或交易日历待核对"],
        }
    expected = sessions(start, end, security.calendar)
    bars, cache = price_bars(s, security.id, cache_id, ranges=[(start, end)])
    selected = {b["date"]: b for b in bars}
    expected_days = {d.isoformat() for d in expected}
    valid = {d for d, b in selected.items() if b["status"] == "VALID"} & expected_days
    missing = [d.isoformat() for d in expected if d.isoformat() not in valid]
    reasons = sorted({b["reason"] for b in selected.values() if b.get("reason")})
    if cache and cache.details.get("reason"):
        reasons.append(cache.details["reason"])
    from iirp.analytics.calendar import last_completed_session

    # Sessions that have not closed yet are pending, not gaps.
    completed = last_completed_session(calendar=security.calendar).isoformat()
    gaps = [d for d in missing if d <= completed]
    return {
        "start_date": str(start),
        "end_date": str(end),
        "expected_sessions": len(expected),
        "available_sessions": len(selected),
        "valid_sessions": len(valid),
        "missing_dates": missing,
        "status": "NOT_FETCHED" if cache is None else "COMPLETE" if not gaps else "PARTIAL" if valid else "NOT_FETCHED",
        "basis": "SPLIT_ONLY" if cache else "UNVERIFIED_PROVIDER_RECORDS",
        "cache_id": cache.id if cache else None,
        "as_of": cache.fetched_at.isoformat() if cache else None,
        "expires_at": cache.expires_at.isoformat() if cache else None,
        "first_valid_date": min(valid) if valid else None,
        "last_valid_date": max(valid) if valid else None,
        "reasons": list(dict.fromkeys(reasons)),
    }


def _today():
    from iirp.analytics.calendar import ET

    return now().astimezone(ET).date()


def ensure_prices(s, scope, security, start, end, *, title=None):
    """Plan at most one fetch so this security's cache covers [start, end].

    Returns the current cache (when it covers the need) and the linked fetch job
    (when one is running or has ended without producing a covering cache).
    """
    from iirp.lifecycle import add_job

    calendar = security.calendar or "XNYS"
    cache = current_cache(s, security.id)
    if covers(cache, start, end, calendar):
        return {"cache": cache, "job": None}
    for job in s.scalars(select(Job).where(
            Job.kind == "market_history", Job.status.in_(ACTIVE),
            Job.target["security_id"].astext == security.id)):
        if job.target["start_date"] <= str(start) and job.target["end_date"] >= str(min(end, _today())):
            add_job(s, scope, job.kind, job.target)
            return {"cache": cache, "job": job}
    # Through today: later needs reuse it, and no future date is requested.
    first, last = start - timedelta(days=BUFFER_DAYS), _today()
    if cache is not None:
        # One wider request replaces the cache; earlier views keep their range.
        first, last = min(first, cache.start_date), max(last, cache.end_date)
    target = {"symbol": security.symbol, "security_id": security.id,
              "start_date": str(first), "end_date": str(last),
              # A new day is a new fetch; the same day reuses a finished one.
              "round": _today().isoformat()}
    job = add_job(s, scope, "market_history", target)
    if title:
        job.title = title
    return {"cache": cache, "job": job}


def fetch_state(ensured):
    """Scope status and wait reason for a price need."""
    job = ensured["job"]
    if job is None:
        return "READY", None
    if job.status in ACTIVE:
        return "RUNNING", None
    return "PARTIAL", job.error or "行情来源未返回可用数据，可重试"


def persist_prices(s, job, response):
    """Replace this security's cache with one validated response.

    The caller owns the lease fence. A narrower response does not replace a
    wider unexpired cache that was saved meanwhile.
    """
    from iirp.analytics.calendar import last_completed_session, previous_session, sessions
    from iirp.market_data import digest, number, price_identity_conflicts, validate_bar

    security = s.get(Security, job.target["security_id"])
    s.execute(text("SELECT pg_advisory_xact_lock(:key)"),
              {"key": int(digest(["price_cache", security.id])[:15], 16)})
    calendar = security.calendar or "XNYS"
    start, end = (date.fromisoformat(job.target[key]) for key in ("start_date", "end_date"))
    provider = response.get("provider", "yfinance")
    conflicts = price_identity_conflicts(s, security, response, provider)
    if conflicts:
        return {"records": 0, "cached": False, "reason": "；".join(conflicts)}
    records = {}
    for record in response.get("records", []):
        day = date.fromisoformat(record["date"])
        if not start <= day <= end:
            continue
        if day in records:
            raise ValueError("来源同一交易日重复，整块保留待核对")
        records[day] = record
    if not records:
        return {"records": 0, "cached": False, "reason": "来源返回空数据；无法据此判断未上市或无交易"}
    completed = last_completed_session(calendar=calendar)
    expected = set(sessions(start, min(end, completed), calendar))
    calendar_days = sessions(previous_session(start, calendar), end, calendar)
    predecessors = dict(zip(calendar_days[1:], calendar_days[:-1]))
    rows, closes, reasons, valid = [], {}, [], set()
    for day, record in sorted(records.items()):
        values, status, reason = validate_bar(record, security, completed)
        if day not in expected and day <= completed:
            status, reason = "INVALID", "来源日期不属于证券交易日历"
        actions = {field: number(record.get(field)) for field in ("dividends", "splits")}
        if any(value is not None and value < 0 for value in actions.values()):
            status, reason = "NEEDS_REVIEW", "公司行动数值不合法，原价格保留待核对"
        if status == "VALID" and security.instrument in ("EQUITY", "ETF"):
            prior_close = closes.get(predecessors.get(day))
            split_boundary = actions["splits"] not in (None, Decimal(0), Decimal(1))
            if prior_close and not split_boundary and abs(values["close"] / prior_close - 1) >= Decimal("0.5"):
                status, reason = "NEEDS_REVIEW", "相邻交易日收盘变化达到 50%，保留原值待核对"
        if status == "VALID":
            closes[day] = values["close"]
            valid.add(day)
        if reason and status != "UNCONFIRMED":
            reasons.append(f"{day}：{reason}")
        rows.append({"session_date": day, **values, **actions, "status": status, "reason": reason})
    missing = sorted(str(day) for day in expected - set(records))
    invalid = sorted(str(day) for day in expected & set(records) - valid)
    existing = s.scalar(select(PriceCache).where(PriceCache.security_id == security.id).with_for_update())
    if (existing is not None and existing.expires_at > now()
            and not (start <= existing.start_date and end >= existing.end_date)):
        return {"records": len(records), "cached": False, "superseded_by": existing.id,
                "message": "已有覆盖更宽区间的缓存，本次结果未替换"}
    if existing is not None:
        s.delete(existing)
        s.flush()
    fetched = now()
    reason = "；".join(dict.fromkeys(reasons))
    cache = PriceCache(
        security_id=security.id, start_date=start, end_date=end, complete_through=completed,
        provider=provider, fetched_at=fetched, expires_at=fetched + timedelta(hours=CACHE_HOURS),
        details={"records": len(records), "missing_dates": missing, "invalid_dates": invalid,
                 "reason": reason, "library_version": response.get("library_version"),
                 "provider_as_of": response.get("fetched_at")},
    )
    s.add(cache)
    s.flush()
    s.execute(insert(PriceCacheBar), [{"cache_id": cache.id, **row} for row in rows])
    return {"records": len(records), "cached": True, "cache_id": cache.id,
            "expires_at": cache.expires_at.isoformat(), "missing_dates": missing,
            "invalid_dates": invalid, "reason": reason}


def expire_price_cache():
    """Delete expired caches and the research results computed from them."""
    from iirp.db import session
    from iirp.maintenance import lock_analysis_references

    removed = {"price_caches": 0, "analysis_results": 0}
    while True:
        with session() as s, s.begin():
            # Same lock as result publication and exports.
            if not lock_analysis_references(s, wait=False):
                return {**removed, "skipped": "analysis_references_busy"}
            stamp = now()
            lapsed = s.execute(select(AnalysisResult.id, AnalysisResult.analysis_id)
                               .where(AnalysisResult.expires_at <= stamp).limit(EXPIRY_BATCH)).all()
            results = [row.id for row in lapsed]
            if results:
                s.execute(delete(AnalysisResult).where(AnalysisResult.id.in_(results)))
                # Opening that research later fetches again (research_freshness).
                s.execute(update(RequestScope).where(RequestScope.batch_id.in_(
                    select(AnalysisRequest.batch_id).where(
                        AnalysisRequest.id.in_({row.analysis_id for row in lapsed}))))
                    .values(checkpoint=RequestScope.checkpoint.op("||")(
                        func.jsonb_build_object("results_expired_at", stamp.isoformat()))))
            caches = s.scalars(select(PriceCache.id).where(PriceCache.expires_at <= stamp)
                               .limit(EXPIRY_BATCH)).all()
            if caches:
                s.execute(delete(PriceCache).where(PriceCache.id.in_(caches)))
        removed["analysis_results"] += len(results)
        removed["price_caches"] += len(caches)
        if len(results) < EXPIRY_BATCH and len(caches) < EXPIRY_BATCH:
            return removed
