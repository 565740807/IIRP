"""Price freshness of a research and the refetch command; reads never call providers."""
from datetime import datetime, timedelta

from sqlalchemy import select, text

from iirp.db import session
from iirp.market.yahoo import digest
from iirp.messages import NotFoundError, msg
from iirp.models import (
    AnalysisRequest,
    AnalysisResult,
    Batch,
    Job,
    PriceCache,
    RequestScope,
    ResearchTrack,
    Security,
    now,
)


def freshness(s, request, items=()):
    """When the prices behind this research were fetched and when they expire.

    Results expire with the price caches they used (D14). ``expired`` means at
    least one result has lapsed, so the page should fetch again.
    """
    from iirp.analysis.calendar import last_completed_session

    batch = s.get(Batch, request.batch_id)
    origin = batch.params.get("research_origin_id", request.id)
    track = s.get(ResearchTrack, origin)
    stamp = now()
    rows = s.execute(select(AnalysisResult.expires_at,
                            AnalysisResult.inputs["price_fetched_at"].astext.label("fetched"))
                     .where(AnalysisResult.analysis_id == request.id)).all()
    live = [row for row in rows if row.expires_at is None or row.expires_at > stamp]
    # The worker deletes lapsed results and marks their scopes.
    deleted = s.scalar(select(RequestScope.id).where(
        RequestScope.batch_id == request.batch_id,
        RequestScope.checkpoint["results_expired_at"].astext.is_not(None)).limit(1))
    fetched = [row.fetched for row in live if row.fetched]
    expires = [row.expires_at for row in live if row.expires_at]
    inputs = s.scalars(select(AnalysisResult.inputs).where(AnalysisResult.analysis_id == request.id)).all()
    cache_ids = {identifier for item in inputs if not item.get("price_sources")
                 for identifier in (item.get("dataset_id"), item.get("benchmark_dataset_id"),
                                    (item.get("benchmark") or {}).get("dataset_id")) if identifier}
    caches = s.execute(select(PriceCache, Security.symbol).join(Security, Security.id == PriceCache.security_id)
                       .where(PriceCache.id.in_(cache_ids)).order_by(PriceCache.expires_at, Security.symbol)).all()
    sources = [{"symbol": symbol, "fetched_at": cache.fetched_at.isoformat(),
                "expires_at": cache.expires_at.isoformat()} for cache, symbol in caches]
    frozen = [source for item in inputs for source in item.get("price_sources", [])]
    missing = cache_ids - {cache.id for cache, _ in caches}
    # Pre-S6d results did not freeze input times. Finished fetches retain the
    # exact deletion timestamp even after a wider response replaces the cache.
    from iirp.market.cache import CACHE_HOURS

    jobs = s.scalars(select(Job).where(Job.kind == "market_history",
                                      Job.result["cache_id"].astext.in_(missing))).all() if missing else []
    for job in jobs:
        expires_at = job.result.get("expires_at")
        if expires_at:
            sources.append({"symbol": job.target["symbol"], "expires_at": expires_at,
                            "fetched_at": (datetime.fromisoformat(expires_at)
                                           - timedelta(hours=CACHE_HOURS)).isoformat()})
    sources = list({(source["symbol"], source["fetched_at"]): source
                    for source in [*sources, *frozen]}.values())
    sources.sort(key=lambda source: (source["expires_at"], source["symbol"]))
    return {"sources": sources, "origin_id": origin, "latest_id": track.latest_id if track else request.id,
        "latest_completed_session": last_completed_session(as_of=stamp).isoformat(),
        "research_cutoff": request.params.get("cutoff_date"),
        "price_fetched_at": min(fetched) if fetched else None,
        "price_expires_at": min(expires) if expires else None,
        "expired": len(live) < len(rows) or deleted is not None,
        "refresh_checked_at": track.checked_at if track else None,
        "note": msg("freshness.cache_note")}


def refresh_analysis(analysis_id, force=False):
    """Fetch again: automatically once results expire, or when asked (``force``).

    The new research keeps the same conditions with the latest completed
    session as its cutoff; caches that still cover it are reused.
    """
    from iirp import models
    from iirp.analysis import requests
    from iirp.api.schemas import AnalysisInput

    with session() as s, s.begin():
        request = s.get(AnalysisRequest, analysis_id)
        if not request:
            raise NotFoundError("analysis.not_found")
        source_batch = s.get(Batch, request.batch_id)
        origin = source_batch.params.get("research_origin_id", request.id)
        s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": "research-refresh:" + origin})
        track = s.get(ResearchTrack, origin)
        if track:
            request = s.get(AnalysisRequest, track.latest_id)
        else:
            track = ResearchTrack(origin_id=origin, latest_id=request.id)
            s.add(track)
        batch = s.get(Batch, request.batch_id)
        current = freshness(s, request)
        # A running fetch answers repeated clicks; otherwise only lapsed results refetch.
        if batch.status in models.ACTIVE or (not force and not current["expired"]):
            track.checked_at = now()
            s.flush()
            return requests.analysis_view(s, request)
        if force:
            import uuid
            command_id = "refetch:" + uuid.uuid4().hex
        else:
            # Repeated automatic checks of the same lapsed research reuse one command.
            command_id = "refetch:" + digest([origin, request.id])
        if request.params["kind"] == "event_dates":
            from iirp.events.service import repeat_analysis
            child_id = repeat_analysis(s, request, command_id).id
        else:
            values = batch.params.get("analysis_input", request.params)
            values = {key: value for key, value in values.items() if key in AnalysisInput.model_fields}
            values["request_id"] = command_id
            created = requests.create_analysis(AnalysisInput.model_validate(values).model_dump(mode="json"), retry_generation=command_id)
            child_id = created["id"]
        child = s.get(AnalysisRequest, child_id)
        child_batch = s.get(Batch, child.batch_id)
        child_batch.params = {**child_batch.params, "research_origin_id": origin}
        child_batch.title = batch.title
        track.latest_id, track.fingerprint, track.checked_at = child.id, command_id[-64:], now()
        s.flush()
        return requests.analysis_view(s, child)
