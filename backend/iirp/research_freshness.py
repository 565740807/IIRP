"""Price freshness of a research and the refetch command; reads never call providers."""
from sqlalchemy import select, text

from iirp.business_models import AnalysisRequest, AnalysisResult, Batch, RequestScope
from iirp.db import session
from iirp.market_data import digest
from iirp.models import now
from iirp.research_tracking import ResearchTrack


def freshness(s, request, items=()):
    """When the prices behind this research were fetched and when they expire.

    Results expire with the price caches they used (D14). ``expired`` means at
    least one result has lapsed, so the page should fetch again.
    """
    from iirp.analytics.calendar import last_completed_session

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
    return {"origin_id": origin, "latest_id": track.latest_id if track else request.id,
        "latest_completed_session": last_completed_session(as_of=stamp).isoformat(),
        "research_cutoff": request.params.get("cutoff_date"),
        "price_fetched_at": min(fetched) if fetched else None,
        "price_expires_at": min(expires) if expires else None,
        "expired": len(live) < len(rows) or deleted is not None,
        "refresh_checked_at": track.checked_at if track else None,
        "note": "行情为 24 小时缓存：获取后 24 小时内重复查看不再下载，过期后重新获取。"}


def refresh_analysis(analysis_id, force=False):
    """Fetch again: automatically once results expire, or when asked (``force``).

    The new research keeps the same conditions with the latest completed
    session as its cutoff; caches that still cover it are reused.
    """
    from iirp import lifecycle
    from iirp.contracts import AnalysisInput

    with session() as s, s.begin():
        request = s.get(AnalysisRequest, analysis_id)
        if not request:
            raise LookupError("分析不存在")
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
        if batch.status in lifecycle.ACTIVE or (not force and not current["expired"]):
            track.checked_at = now()
            s.flush()
            return lifecycle.analysis_view(s, request)
        if force:
            import uuid
            command_id = "refetch:" + uuid.uuid4().hex
        else:
            # Repeated automatic checks of the same lapsed research reuse one command.
            command_id = "refetch:" + digest([origin, request.id])
        if request.params["kind"] == "event_dates":
            from iirp.analytics.calendar import last_completed_session
            from iirp.event_api import EventAnalysisInput
            from iirp.event_service import create_analysis
            values = {key: value for key, value in request.params.items() if key in EventAnalysisInput.model_fields}
            values.update(request_id=command_id, version=request.params["event_version"],
                          cutoff_date=last_completed_session(as_of=now()).isoformat(),
                          retry_generation=command_id)
            created = create_analysis(request.params["event_set_id"], values)
            child_id = created["analysis_id"]
        else:
            values = batch.params.get("analysis_input", request.params)
            values = {key: value for key, value in values.items() if key in AnalysisInput.model_fields}
            values["request_id"] = command_id
            created = lifecycle.create_analysis(AnalysisInput.model_validate(values).model_dump(mode="json"), retry_generation=command_id)
            child_id = created["id"]
        child = s.get(AnalysisRequest, child_id)
        child_batch = s.get(Batch, child.batch_id)
        child_batch.params = {**child_batch.params, "research_origin_id": origin}
        child_batch.title = batch.title
        track.latest_id, track.fingerprint, track.checked_at = child.id, command_id[-64:], now()
        s.flush()
        return lifecycle.analysis_view(s, child)
