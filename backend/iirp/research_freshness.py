"""Explicit durable refresh commands; reads never invoke external providers."""
from sqlalchemy import func, select, text

from iirp.business_models import AnalysisRequest, AnalysisResult, Batch, CoverageSegment, Security
from iirp.db import session
from iirp.market_data import digest
from iirp.models import now
from iirp.research_tracking import ResearchTrack


def freshness(s, request, items):
    from iirp.analytics.calendar import last_completed_session
    batch = s.get(Batch, request.batch_id)
    origin = batch.params.get("research_origin_id", request.id)
    track = s.get(ResearchTrack, origin)
    checked = s.scalar(select(func.max(CoverageSegment.checked_at)).where(
        CoverageSegment.security_id.in_([item["security_id"] for item in items]))) if items else None
    return {"origin_id": origin, "latest_id": track.latest_id if track else request.id,
        "latest_completed_session": last_completed_session(as_of=now()).isoformat(),
        "research_cutoff": request.params.get("cutoff_date"),
        "data_verified_at": checked, "refresh_checked_at": track.checked_at if track else None,
        "note": "盘中报价不等于已完成日线；结果截止日期按每个不可变版本显示。"}


def refresh_analysis(analysis_id, force=False):
    from iirp import lifecycle
    from iirp.analytics.calendar import last_completed_session
    from iirp.contracts import AnalysisInput
    from iirp.market_data import latest_dataset

    # Serialize only this research's refresh commands. A crash can leave a new
    # child request, but its deterministic request_id makes the retry reusable.
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
        cutoff = last_completed_session(as_of=lifecycle.now()).isoformat()
        view = lifecycle.analysis_view(s, request)
        current = bool(view["results"]) and len(view["results"]) == len(view["batch"]["items"]) and all(r["is_current"] and (r.get("coverage") or {}).get("complete") for r in view["results"])
        if request.params.get("cutoff_date") == cutoff and (current or batch.status in lifecycle.ACTIVE):
            track.checked_at = now()
            s.flush()
            return lifecycle.analysis_view(s, request)
        # Dependency keys include facts, calendars, basis and calculation version.
        keys = []
        for scope in view["batch"]["items"]:
            security = s.get(Security, scope.get("security_id")) if scope.get("security_id") else s.scalar(select(Security).where(Security.symbol == scope["symbol"]))
            dataset = latest_dataset(s, security.id) if security else None
            if security and dataset:
                from iirp.business_models import EarningsEvent
                events = s.scalars(select(EarningsEvent).where(EarningsEvent.security_id == security.id)).all() if request.params["kind"] == "earnings" else []
                keys.append(lifecycle.research_input_key(request, dataset, events, security))
        fingerprint = digest([origin, cutoff, keys])
        if track.fingerprint == fingerprint and not force:
            track.checked_at = now()
            return lifecycle.analysis_view(s, request)
        if force:
            import uuid
            command_id = "follow-latest:" + uuid.uuid4().hex
        else:
            command_id = "follow-latest:" + fingerprint
        if request.params["kind"] == "event_dates":
            from iirp.event_api import EventAnalysisInput
            from iirp.event_service import create_analysis
            values = {key: value for key, value in request.params.items() if key in EventAnalysisInput.model_fields}
            values.update(request_id=command_id, version=request.params["event_version"], cutoff_date=cutoff, retry_generation=command_id)
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
        # Carry each previous readable immutable result until this ticker is
        # ready. Its data, inputs and cutoff remain the old version in all exports.
        existing = set(s.scalars(select(AnalysisResult.security_id).where(AnalysisResult.analysis_id == child.id)))
        for item in view["results"]:
            if item["security_id"] in existing:
                continue
            old = s.get(AnalysisResult, item["result_id"])
            s.add(AnalysisResult(analysis_id=child.id, security_id=old.security_id,
                input_key=old.input_key, inputs={**old.inputs, "params": old.inputs.get("params", request.params), "carried_from": old.id}, data=old.data))
        track.latest_id, track.fingerprint, track.checked_at = child.id, fingerprint, now()
        s.flush()
        return lifecycle.analysis_view(s, child)
